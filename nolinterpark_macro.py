# -*- coding: utf-8 -*-

import io
import re
import sys
import time
import threading
import traceback
import winsound

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
from PIL import Image
import numpy as np

from PySide6.QtCore import Qt, QThread, Signal, QObject
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QSpinBox, QPushButton,
    QButtonGroup, QTextEdit, QMessageBox
)

EXPIRE_DATE = "~26.09.30"
LOGIN_URL   = (
    "https://accounts.yanolja.com/v3/login"
    "?clientId=inpark-pc&postProc=FULLSCREEN"
    "&origin=https%3A%2F%2Fnol.interpark.com%2F"
    "&loginTab=NON_MEMBER_MY_BOOKING&service=interpark-integrate"
    "&nol_device_id=178123879502153851"
    "&redirect=aHR0cHM6Ly9hY2NvdW50cy5pbnRlcnBhcmsuY29tL2xvZ2luL3N1Y2Nlc3Mvbm9sP3Bvc3RQcm9jPUZVTExTQ1JFRU4mb3JpZ2luPWh0dHBzJTNBJTJGJTJGbm9sLmludGVycGFyay5jb20lMkY"
)
GRADES = ["스탠딩R", "스탠딩S", "지정석R", "지정석S", "지정석A", "지정석B"]  # fallback only

# 공연마다 등급/색상/구역이 다르므로 모두 페이지에서 동적으로 읽어온다.


# ───────────────────────────────────────────────
#  신호 브릿지
# ───────────────────────────────────────────────
class Signals(QObject):
    log_signal   = Signal(str)
    show_input   = Signal()
    hide_input   = Signal()


# ───────────────────────────────────────────────
#  매크로 스레드
# ───────────────────────────────────────────────
class MacroThread(QThread):

    def __init__(self, driver, delay, signals):
        super().__init__()
        self.driver  = driver
        self.delay   = delay
        self.sig     = signals
        self._stop   = False
        self._pause  = False
        self._ev     = threading.Event()
        self._answer = ""

    def stop(self):
        self._stop = True

    def toggle_pause(self):
        self._pause = not self._pause
        return self._pause

    def set_answer(self, text):
        self._answer = text
        self._ev.set()

    # ── 로그 ──────────────────────────────────
    def log(self, msg):
        ts = time.strftime("%Y/%m/%d %H:%M:%S")
        self.sig.log_signal.emit(f"[{ts}] {msg}")

    # ── 대기 ──────────────────────────────────
    def _wait(self, sec):
        # sec=0 이어도 정지/일시정지를 최소 한 번은 확인하고 빠져나간다.
        end = time.time() + sec
        while True:
            if self._stop: raise InterruptedError
            while self._pause:
                if self._stop: raise InterruptedError
                time.sleep(0.1)
            if time.time() >= end:
                return
            time.sleep(0.05)

    # ── GUI 입력 요청 ─────────────────────────
    def _ask(self, timeout=120):
        self._ev.clear()
        self.sig.show_input.emit()
        ok  = self._ev.wait(timeout=timeout)
        ans = self._answer
        self.sig.hide_input.emit()
        return ans if ok else None

    # ── URL / 소스 헬퍼 ──────────────────────
    def _url(self):
        try: return self.driver.current_url
        except: return ""

    def _src(self):
        try: return self.driver.page_source
        except: return ""

    # ── 로그인 완료 감지 ──────────────────────
    # 로그인 페이지(accounts.*)를 벗어나 서비스 도메인(nol/poticket interpark,
    # nol.yanolja, myaccount)에 도달하면 로그인 완료로 간주한다.
    def _is_logged_in(self):
        url = self._url()
        if not url:
            return False
        # 아직 로그인/계정 인증 페이지에 있으면 미완료
        if "accounts.yanolja.com" in url or "accounts.interpark.com" in url:
            return False
        return (
            "nol.interpark.com" in url
            or "poticket.interpark.com" in url
            or "ticket.interpark.com" in url
            or "nol.yanolja.com" in url
            or "myaccount" in url
        )

    # ── 안심예매 보안문자(캡챠) 감지 ───────────
    # poticket BookMain.asp 는 중첩 iframe 구조라 모든 프레임을 재귀로 훑는다.
    # 보이는 캡챠 입력창 또는 안심예매 안내문이 발견되면 True.
    def _captcha_present(self):
        drv = self.driver
        try:
            drv.switch_to.default_content()
        except Exception:
            return False
        found = self._scan_captcha(0)
        try:
            drv.switch_to.default_content()
        except Exception:
            pass
        return found

    def _scan_captcha(self, depth):
        drv = self.driver
        # 1) 현재 프레임에서 캡챠 입력창 탐지 (넓은 셀렉터)
        input_sels = [
            "input[placeholder*='문자']",
            "input[id*='aptcha']",
            "input[name*='aptcha']",
            "#txtCaptcha", "#captchaInput",
            ".captcha_input input", ".capchaInner input",
        ]
        for sel in input_sels:
            try:
                for el in drv.find_elements(By.CSS_SELECTOR, sel):
                    if el.is_displayed():
                        return True
            except Exception:
                pass
        # 2) 텍스트 기반 (안심예매 보안문자 안내문)
        try:
            src = drv.page_source
            if "부정예매방지" in src and "문자를 입력" in src:
                return True
        except Exception:
            pass
        if depth >= 3:
            return False
        # 3) 하위 iframe 재귀 탐색
        try:
            cnt = len(drv.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            cnt = 0
        for i in range(cnt):
            try:
                frames = drv.find_elements(By.TAG_NAME, "iframe")
                if i >= len(frames):
                    break
                drv.switch_to.frame(frames[i])
                if self._scan_captcha(depth + 1):
                    return True
                drv.switch_to.parent_frame()
            except Exception:
                try:
                    drv.switch_to.parent_frame()
                except Exception:
                    try: drv.switch_to.default_content()
                    except Exception: pass
        return False

    # ── 안심예매 보안문자 통과 대기 ────────────
    # 자동 입력은 중첩 iframe/캡챠 변형에 취약하므로, 사용자가 예매창에서
    # 직접 보안문자를 입력하도록 안내하고 캡챠가 사라질 때까지 대기한다.
    def _wait_captcha_cleared(self, timeout=300):
        self.log("안심예매 보안문자가 떴습니다.")
        self.log("→ 예매창에서 직접 보안문자를 입력하고 [입력완료]를 눌러주세요.")
        self.log("  통과되면 자동으로 다음 단계로 진행합니다...")
        end = time.time() + timeout
        while time.time() < end:
            self._wait(1.0)
            if not self._captcha_present():
                self.log("→ 보안문자 통과 확인")
                return True
        self.log("보안문자 대기 시간 초과")
        return False

    # ── 텍스트 버튼 클릭 헬퍼 ─────────────────
    # labels 중 화면에 보이는 첫 요소를 클릭. 클릭한 라벨 반환(없으면 None)
    def _click_text_button(self, labels):
        drv = self.driver
        js = r"""
        var labels = arguments[0];
        var nodes = document.querySelectorAll('a,button,span,div,li,p');
        for (var i=0;i<nodes.length;i++){
            var t=(nodes[i].textContent||'').replace(/\s+/g,'').trim();
            for (var k=0;k<labels.length;k++){
                if (t===labels[k]){
                    var b=nodes[i].getBoundingClientRect();
                    if (b.width>0 && b.height>0){ nodes[i].click(); return labels[k]; }
                }
            }
        }
        return null;
        """
        try: return drv.execute_script(js, labels)
        except: return None

    # ── 좌석가격(등급) 패널 열기 ─────────────
    def _open_price_panel(self):
        # '가격 전체보기' 패널을 연다(등급별 구역 링크가 나열됨). 모든 프레임 시도.
        for xp in [
            "//*[contains(text(),'가격 전체보기')]",
            "//*[contains(text(),'가격전체보기')]",
            "//*[contains(text(),'좌석가격보기')]",
            "//*[contains(text(),'가격보기')]",
        ]:
            if self._open_price_panel_recursive(xp, 0):
                return True
        return False

    def _open_price_panel_recursive(self, xp, depth):
        drv = self.driver
        try:
            for el in drv.find_elements(By.XPATH, xp):
                try:
                    if el.is_displayed():
                        drv.execute_script("arguments[0].click();", el)
                        self._wait(0.6)
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        if depth >= 3:
            return False
        try:
            cnt = len(drv.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            cnt = 0
        for i in range(cnt):
            try:
                frames = drv.find_elements(By.TAG_NAME, "iframe")
                if i >= len(frames):
                    break
                drv.switch_to.frame(frames[i])
                if self._open_price_panel_recursive(xp, depth + 1):
                    return True
                drv.switch_to.parent_frame()
            except Exception:
                try: drv.switch_to.parent_frame()
                except Exception:
                    try: drv.switch_to.default_content()
                    except Exception: pass
        return False

    # ── 좌석가격(등급) 패널 닫기 ─────────────
    # 가격표를 읽은 뒤 '가격닫기'를 눌러 좌석배치도로 복귀시킨다.
    def _close_price_panel(self):
        # 1) '가격닫기' 버튼 클릭 (가격표 열려있을 때만 존재)
        if self._click_text_button(["가격닫기"]):
            self._wait(0.4)
            return True
        # 2) JS로 가격 패널 요소 직접 숨김 (폴백)
        js = r"""
        var sels=['.price_view','.seat_price','[class*="priceView"]',
                  '[class*="seatPrice"]','[class*="price_layer"]','[class*="legend"]'];
        var hidden=false;
        for(var s=0;s<sels.length;s++){
            var els=document.querySelectorAll(sels[s]);
            for(var i=0;i<els.length;i++){
                els[i].style.display='none'; hidden=true;
            }
        }
        return hidden;
        """
        try: return bool(self.driver.execute_script(js))
        except: return False

    # ── 잔여좌석 안내 패널 닫기 (좌석 잡은 뒤) ─
    # 좌석 선택 후 뜨는 '잔여좌석 안내' 패널을 '좌석닫기'로 닫는다.
    def _close_seat_panel(self):
        if self._click_text_button(["좌석닫기"]):
            self._wait(0.4)
            return True
        return False

    # ── 모든 프레임 재귀 실행 헬퍼 ────────────
    # 현재 컨텍스트부터 하위 iframe까지 재귀로 fn()을 실행해 결과 리스트를 누적.
    # (poticket BookMain.asp 처럼 좌석/등급이 중첩 iframe 안에 있을 때 사용)
    def _collect_all_frames(self, fn, depth=0):
        drv = self.driver
        try:
            out = list(fn() or [])
        except Exception:
            out = []
        if depth >= 4:
            return out
        try:
            cnt = len(drv.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            cnt = 0
        for i in range(cnt):
            try:
                frames = drv.find_elements(By.TAG_NAME, "iframe")
                if i >= len(frames):
                    break
                drv.switch_to.frame(frames[i])
                out += self._collect_all_frames(fn, depth + 1)
                drv.switch_to.parent_frame()
            except Exception:
                try:
                    drv.switch_to.parent_frame()
                except Exception:
                    try: drv.switch_to.default_content()
                    except Exception: pass
        return out

    # ── 등급 목록 + 색상 동적 읽기 ────────────
    # 가격 패널을 열지 않고 현재 DOM에서 등급 행을 스캔한다.
    def _scan_grades(self):
        js = r"""
        var out = [], seen = {};
        var all = document.querySelectorAll('li,tr,div,p,span,dt,dd,td');
        for (var i=0;i<all.length;i++){
            var el = all[i];
            var txt = (el.textContent||'').replace(/\s+/g,' ').trim();
            if (txt.length>40) continue;
            if (!/석|존/.test(txt)) continue;        // 등급명에 '석' 또는 '존'
            if (!/원/.test(txt)) continue;           // 가격 포함 행
            var name = txt.replace(/[\d,]+\s*원.*/,'').replace(/\d+\s*석/g,'').replace(/잔여|매진/g,'').replace(/\s+/g,' ').trim();
            if (!name || name.length<2 || seen[name]) continue;
            // 색상 swatch (배경색) 찾기
            var color = null;
            var scan = [el].concat([].slice.call(el.querySelectorAll('*')));
            for (var j=0;j<scan.length;j++){
                var cs = getComputedStyle(scan[j]);
                var bg = cs.backgroundColor||'';
                var m = bg.match(/(\d+),\s*(\d+),\s*(\d+)/);
                if (m){
                    var r=+m[1],g=+m[2],b=+m[3];
                    if (r>245&&g>245&&b>245) continue;   // 흰색 제외
                    if (r<12&&g<12&&b<12) continue;      // 검정 제외
                    color = [r,g,b]; break;
                }
            }
            seen[name]=1;
            out.push({name:name, color:color});
        }
        return out;
        """
        try:
            return self.driver.execute_script(js) or []
        except:
            return []

    # ── 등급 목록 읽기 (모든 프레임 재귀 스캔) ──
    def _dedup_grades(self, rows):
        seen, out = set(), []
        for r in rows:
            n = (r or {}).get('name')
            if n and n not in seen:
                seen.add(n); out.append(r)
        return out

    def _get_grades(self):
        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        rows = self._dedup_grades(self._collect_all_frames(self._scan_grades))
        try: drv.switch_to.default_content()
        except Exception: pass
        if rows:
            return rows
        # 폴백: 가격 패널을 열어 다시 시도
        self._open_price_panel()
        self._wait(0.4)
        try: drv.switch_to.default_content()
        except Exception: pass
        rows = self._dedup_grades(self._collect_all_frames(self._scan_grades))
        try: drv.switch_to.default_content()
        except Exception: pass
        self._close_price_panel()
        return rows

    # ── 좌석 등급 선택 ───────────────────────
    def _ask_grade(self):
        grades = self._get_grades()
        if not grades:
            self.log("등급 정보를 읽지 못했습니다 → 모두로 진행")
            return None, []
        self.log("좌석 등급을 입력해주세요:")
        self.log("1. 모두")
        for i, g in enumerate(grades, 2):
            self.log(f"{i}. {g['name']}")
        ans = self._ask(timeout=120)
        if ans is None:
            self.log("시간 초과 → 모두로 진행"); return None, grades
        self.log(f"→ {ans}")
        try: n = int(ans)
        except: return None, grades
        if n == 1: return None, grades
        if 2 <= n <= len(grades) + 1:
            sel = grades[n - 2]
            self.log(f"선택 등급: {sel['name']}")
            return sel, grades
        return None, grades

    # ── iframe 전환 헬퍼 ──────────────────────
    def _to_frame(self, *ids):
        self.driver.switch_to.default_content()
        for fid in ids:
            try:
                self.driver.switch_to.frame(
                    self.driver.find_element(By.ID, fid)
                )
                return True
            except: pass
        return False

    # ── 구역 목록 동적 읽기 (label + color) ─────
    # 반환: [{'label': '101', 'color': [r,g,b] or None}, ...]
    # poticket BookMain.asp 는 구역이 중첩 iframe(이미지맵 area 태그 또는
    # SVG/HTML) 안에 있으므로 모든 프레임을 재귀로 훑는다.
    def _get_zones(self):
        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        allz = self._collect_all_frames(self._scan_zones_current_frame)
        try: drv.switch_to.default_content()
        except Exception: pass
        # 이미지맵(area 태그)로 찾은 게 있으면 우선 사용(가장 정확), 없으면 JS 결과
        area = [z for z in allz if z.get('src') == 'area']
        zones = self._dedup_zones(area if area else allz)
        return self._sort_zones(zones)

    # 구역을 라벨 속 숫자 기준 오름차순 정렬 (숫자 없으면 뒤로)
    def _sort_zones(self, zones):
        def key(z):
            m = re.search(r"\d+", z.get('label', ''))
            return (0, int(m.group())) if m else (1, z.get('label', ''))
        try:
            return sorted(zones, key=key)
        except Exception:
            return zones

    # ── area(구역) 경계 박스 계산 (natural 좌표) ──
    def _area_bbox(self, shape, coords):
        try:
            nums = [float(x) for x in re.split(r"[ ,]+", (coords or '').strip()) if x != '']
        except Exception:
            return None
        if not nums:
            return None
        shape = (shape or '').lower()
        if shape == 'circle' and len(nums) >= 3:
            cx, cy, r = nums[0], nums[1], nums[2]
            return (cx - r, cy - r, cx + r, cy + r)
        if shape == 'rect' and len(nums) >= 4:
            return (min(nums[0], nums[2]), min(nums[1], nums[3]),
                    max(nums[0], nums[2]), max(nums[1], nums[3]))
        xs, ys = nums[0::2], nums[1::2]
        if xs and ys:
            return (min(xs), min(ys), max(xs), max(ys))
        return None

    # ── 좌석배치도 이미지에서 각 구역의 채움색 샘플링 ──
    # 구역 영역 안을 격자로 찍어 가장 많이 나온 유채색을 그 구역 색으로 본다.
    # 반환: areas 와 같은 길이의 [r,g,b] 또는 None 리스트
    def _sample_area_colors(self, areas):
        drv = self.driver
        colors = [None] * len(areas)
        # usemap 이미지(좌석배치도) 찾기 - 가장 큰 것
        img = None
        try:
            imgs = drv.find_elements(By.CSS_SELECTOR, "img[usemap]")
            if imgs:
                img = max(imgs, key=lambda e: (e.size.get('width', 0) *
                                               e.size.get('height', 0)) if e.size else 0)
        except Exception:
            img = None
        if img is None:
            return colors
        try:
            im = Image.open(io.BytesIO(img.screenshot_as_png)).convert("RGB")
        except Exception:
            return colors
        sw, sh = im.size
        try:
            nat = drv.execute_script(
                "var i=arguments[0];return [i.naturalWidth,i.naturalHeight];", img)
            nw = nat[0] or sw
            nh = nat[1] or sh
        except Exception:
            nw, nh = sw, sh
        sx = sw / float(nw) if nw else 1.0
        sy = sh / float(nh) if nh else 1.0
        px = im.load()

        def colored(rgb):
            r, g, b = rgb[0], rgb[1], rgb[2]
            if r > 235 and g > 235 and b > 235:           # 흰색/배경
                return False
            if max(r, g, b) - min(r, g, b) < 22 and 90 < min(r, g, b) < 215:  # 회색
                return False
            return True

        for idx, a in enumerate(areas):
            box = self._area_bbox(a.get('shape', ''), a.get('coords', ''))
            if not box:
                continue
            x0, y0, x1, y1 = (box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy)
            # 테두리/라벨 영향을 줄이려 중앙 60% 영역만 격자 샘플링
            w, h = x1 - x0, y1 - y0
            ax0, ay0 = x0 + w * 0.2, y0 + h * 0.2
            ax1, ay1 = x1 - w * 0.2, y1 - h * 0.2
            counts = {}
            steps = 6
            for ix in range(steps + 1):
                for iy in range(steps + 1):
                    x = int(ax0 + (ax1 - ax0) * ix / steps)
                    y = int(ay0 + (ay1 - ay0) * iy / steps)
                    if 0 <= x < sw and 0 <= y < sh:
                        rgb = px[x, y]
                        if colored(rgb):
                            key = (rgb[0] // 12 * 12, rgb[1] // 12 * 12, rgb[2] // 12 * 12)
                            counts[key] = counts.get(key, 0) + 1
            if counts:
                best = max(counts.items(), key=lambda kv: kv[1])[0]
                colors[idx] = [best[0], best[1], best[2]]
        return colors

    # ── 한 프레임 안에서 구역 수집 ─────────────
    # (1) area 태그 이미지맵 (2) JS 텍스트/도형 스캔
    def _scan_zones_current_frame(self):
        drv = self.driver
        zones = []
        # (1) area 태그 (BookMain.asp 좌석 이미지맵)
        # 구역 번호는 이미지에 그려져 DOM 텍스트엔 없는 경우가 많다. title/alt 가
        # 있으면 그걸, 없으면 href 의 숫자를 라벨로 쓴다. 클릭은 href 실행이 확실.
        # (등급 매칭은 색이 아니라 구역에 들어가 좌석 등급을 직접 읽어 판단)
        try:
            for a in drv.find_elements(By.TAG_NAME, "area"):
                title  = (a.get_attribute("title") or a.get_attribute("alt") or "").strip()
                href   = (a.get_attribute("href") or "").strip()
                label = title
                if not label and href:
                    m = re.findall(r"\d{1,4}", href)
                    if m:
                        label = m[-1]
                if not label:
                    continue
                zones.append({'label': label, 'href': href,
                              'color': None, 'src': 'area'})
        except Exception:
            pass
        # (2) SVG/HTML 텍스트+도형 스캔
        js = r"""
        function toRGB(s){
            if(!s) return null;
            var m=s.match(/(\d+),\s*(\d+),\s*(\d+)/);
            if(m) return [+m[1],+m[2],+m[3]];
            var h=s.replace(/^#/,'');
            if(h.length===3) h=h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
            if(h.length===6) return [parseInt(h.slice(0,2),16),
                                     parseInt(h.slice(2,4),16),
                                     parseInt(h.slice(4,6),16)];
            return null;
        }
        function colorOf(el){
            if(!el) return null;
            var cs=window.getComputedStyle(el);
            var cands=[el.getAttribute&&el.getAttribute('fill'), cs.fill,
                       cs.backgroundColor, (el.style&&el.style.fill)];
            for(var k=0;k<cands.length;k++){
                var rgb=toRGB(cands[k]||'');
                if(!rgb) continue;
                var r=rgb[0],g=rgb[1],b=rgb[2];
                if(r>245&&g>245&&b>245) continue;   // 흰색
                if(r<12&&g<12&&b<12) continue;      // 검정
                return rgb;
            }
            return null;
        }
        // 라벨 노드 기준으로 자신→형제도형→부모 순서로 색을 찾음
        function findColor(textEl){
            var node=textEl;
            for(var d=0; d<5 && node; d++){
                var c=colorOf(node);
                if(c) return c;
                var par=node.parentElement;
                if(par){
                    var shapes=par.querySelectorAll('rect,polygon,path,circle');
                    for(var i=0;i<shapes.length;i++){
                        var cc=colorOf(shapes[i]);
                        if(cc) return cc;
                    }
                }
                node=node.parentElement;
            }
            return null;
        }
        var out=[], seen={};
        var nodes=document.querySelectorAll('text,tspan,a,g,span,div,li,td');
        for(var i=0;i<nodes.length;i++){
            var t=(nodes[i].textContent||'').trim();
            if(/^[A-Z가-힣]?\d{1,3}$/.test(t)||/^[A-Z가-힣]$/.test(t)){
                if(seen[t]) continue;
                seen[t]=1;
                out.push({label:t, color:findColor(nodes[i])});
            }
        }
        return out;
        """
        try:
            for z in (drv.execute_script(js) or []):
                z['src'] = 'js'
                zones.append(z)
        except Exception:
            pass
        return zones

    def _dedup_zones(self, items):
        # href가 있으면 href로(서로 다른 area 모두 유지), 없으면 label로 중복 제거
        seen, out = set(), []
        for x in items:
            key = x.get('href') or x.get('label', '')
            if key and key not in seen:
                seen.add(key); out.append(x)
        return out

    def _dedup(self, items):
        seen, out = set(), []
        for x in items:
            if x not in seen:
                seen.add(x); out.append(x)
        return out

    # ── 펼쳐진 패널에서 'N영역' 링크 읽기 JS ───
    _JS_READ_ZONES = r"""
    var out=[];
    var nodes=document.querySelectorAll('a');
    for(var i=0;i<nodes.length;i++){
        var el=nodes[i];
        var m=(el.textContent||'').match(/(\d{1,4})\s*영역/);
        if(m){
            var b=el.getBoundingClientRect();
            if(b.width>0 && b.height>0){
                out.push({label:m[1], href:el.getAttribute('href')||'',
                          onclick:el.getAttribute('onclick')||''});
            }
        }
    }
    return out;
    """

    # ── 요소 안전 클릭 (네이티브 우선, JS 폴백) ──
    def _safe_click(self, el):
        drv = self.driver
        try:
            drv.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        except Exception:
            pass
        try:
            el.click()
            return True
        except Exception:
            try:
                drv.execute_script("arguments[0].click();", el)
                return True
            except Exception:
                return False

    # ── 선택 등급의 구역 읽기 (좌석등급 패널) ──
    # 좌석등급 패널에서 선택 등급명을 가진 요소를 '네이티브 클릭'해 펼친 뒤,
    # 그때 나타나는 'N영역' 링크들을 읽는다. (모든 프레임 재귀)
    def _get_zones_for_grade(self, gname, depth=0):
        drv = self.driver
        g = (gname or "").replace(" ", "")
        # 1) 현재 프레임: 등급명을 가진 짧은 텍스트 요소들을 후보로 모아 클릭 시도
        cands = []
        try:
            xp = "//*[contains(normalize-space(.), '%s')]" % gname
            for el in drv.find_elements(By.XPATH, xp):
                try:
                    t = (el.text or "").replace(" ", "")
                    if t and len(t) <= 40 and g in t:
                        cands.append((len(t), el))
                except Exception:
                    continue
        except Exception:
            pass
        cands.sort(key=lambda x: x[0])   # 짧은 텍스트(등급명 자체) 우선
        for _, el in cands[:6]:
            try:
                self._safe_click(el)
                self._wait(0.7)
                for _r in range(2):
                    try:
                        zs = drv.execute_script(self._JS_READ_ZONES) or []
                    except Exception:
                        zs = []
                    if zs:
                        return zs
                    self._wait(0.4)
            except Exception:
                continue
        if depth >= 4:
            return []
        # 2) 하위 프레임 재귀
        try:
            cnt = len(drv.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            cnt = 0
        for i in range(cnt):
            try:
                frames = drv.find_elements(By.TAG_NAME, "iframe")
                if i >= len(frames):
                    break
                drv.switch_to.frame(frames[i])
                zs = self._get_zones_for_grade(gname, depth + 1)
                if zs:
                    return zs
                drv.switch_to.parent_frame()
            except Exception:
                try: drv.switch_to.parent_frame()
                except Exception:
                    try: drv.switch_to.default_content()
                    except Exception: pass
        return []

    # ── 등급 패널 구조 진단 (구역 못 읽을 때) ──
    def _diagnose_grade_panel(self, gname):
        g = (gname or "").replace(" ", "")
        js = r"""
        var g=arguments[0];
        function norm(s){return (s||'').replace(/\s+/g,'');}
        var gradeHits=[], areaCnt=0;
        var nodes=document.querySelectorAll('a,li,dd,dt,span,div,td,p,strong,b');
        for(var i=0;i<nodes.length;i++){
            var raw=(nodes[i].textContent||'');
            if(/(\d{1,4})\s*영역/.test(raw)) areaCnt++;
            var t=norm(raw);
            if(t.length>0 && t.length<=24 && g && t.indexOf(g)>=0 && gradeHits.length<4){
                gradeHits.push(nodes[i].tagName+':'+t.slice(0,24));
            }
        }
        return {grade:gradeHits, area:areaCnt};
        """
        def run():
            try:
                r = self.driver.execute_script(js, g)
                return [r] if r and (r.get('grade') or r.get('area')) else []
            except Exception:
                return []
        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        rows = self._collect_all_frames(run)
        try: drv.switch_to.default_content()
        except Exception: pass
        if rows:
            self.log("[진단] 등급/구역 패널:")
            for r in rows[:4]:
                self.log(f"  등급매칭 {r.get('grade')}, 영역링크수={r.get('area')}")
        else:
            self.log(f"[진단] '{gname}' 패널 요소를 못 찾음")

    # ── 구역 선택 (등급 클릭 후 펼쳐진 구역만 표시; 색 미사용) ──
    def _ask_zones(self, grade=None, grade_list=None):
        gname = (grade or {}).get('name', '')
        all_zones = self._get_zones()
        zone_list = all_zones

        if gname:
            # 1) 좌석등급 패널에서 선택 등급을 클릭해 펼친 뒤 구역을 읽는다
            zs = self._get_zones_for_grade(gname)
            # 2) 비면 '가격 전체보기'를 열고 재시도
            if not zs:
                self._open_price_panel(); self._wait(0.6)
                zs = self._get_zones_for_grade(gname)
            try: self.driver.switch_to.default_content()
            except Exception: pass
            if not zs:
                self._diagnose_grade_panel(gname)   # 구조 진단
            if zs:
                zlist = []
                for z in zs:
                    href = z.get('href') or ''
                    if not href and z.get('onclick'):
                        href = 'javascript:' + z['onclick']
                    zlist.append({'label': z.get('label', ''), 'href': href,
                                  'color': None, 'src': 'panel'})
                zone_list = self._dedup_zones(zlist)
                self.log(f"[{gname}] 구역 {len(zone_list)}개 (가격표에서 매핑)")
            else:
                self.log(f"'{gname}' 구역 매핑을 못 읽음 → 전체 구역 표시")
                zone_list = all_zones

        zone_list = self._sort_zones(zone_list)
        if not zone_list:
            self.log("구역 목록을 읽지 못했습니다. 직접 입력하세요 (예: 105,106)")
        else:
            for i, z in enumerate(zone_list, 1):
                lbl = z.get('label', '')
                self.log(f"{i}. {lbl if '영역' in lbl else lbl + '영역'}")
        self.log("구역 번호 입력(','로 여러개). 그냥 Enter 시 전체 구역을 순회합니다.")
        ans = self._ask(timeout=120)
        if not ans:
            return zone_list            # 전체 구역 순회
        self.log(f"→ {ans}")
        selected = []
        for part in ans.split(","):
            part = part.strip()
            if not part: continue
            try:
                idx = int(part) - 1
                if 0 <= idx < len(zone_list):
                    selected.append(zone_list[idx])
                else:
                    selected.append({'label': part, 'href': '', 'color': None})
            except:
                selected.append({'label': part, 'href': '', 'color': None})
        return selected if selected else zone_list

    # ── 좌석 클릭 ─────────────────────────────
    # grade: {'name','color':[r,g,b]} 또는 None(모두)
    def _click_seat(self, grade=None):
        drv = self.driver
        target = (grade or {}).get("color")  # [r,g,b] 또는 None

        # 이미 좌석이 선택돼 있으면(총 N석>0) 추가 클릭 없이 성공 처리
        if self._seat_selected() is True:
            self.log("→ 좌석 이미 선택됨")
            return True

        js = r"""
        var target = arguments[0];   // [r,g,b] 또는 null
        function parseColor(s){
            if(!s||s==='none'||s==='transparent') return null;
            var m=s.match(/(\d+),\s*(\d+),\s*(\d+)/);
            if(m) return {r:+m[1],g:+m[2],b:+m[3]};
            var h=s.replace(/^#/,'');
            if(h.length===3) h=h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
            if(h.length===6) return {
                r:parseInt(h.slice(0,2),16),
                g:parseInt(h.slice(2,4),16),
                b:parseInt(h.slice(4,6),16)};
            return null;
        }
        function getRGB(el){
            try {
                var cs = window.getComputedStyle(el);
                // 1) SVG fill attribute
                var r = parseColor(el.getAttribute('fill')||'');
                if(r) return r;
                // 2) CSS fill (SVG)
                r = parseColor(cs.fill||'');
                if(r) return r;
                // 3) background-color (td/div 등 일반 요소)
                r = parseColor(cs.backgroundColor||'');
                if(r) return r;
                // 4) inline style backgroundColor
                r = parseColor((el.style&&el.style.backgroundColor)||'');
                if(r) return r;
            } catch(e){}
            return null;
        }
        function isGray(r,g,b){
            return Math.max(r,g,b)-Math.min(r,g,b)<28 && Math.min(r,g,b)>110;
        }
        function matchColor(rgb){
            if(!rgb) return false;
            if(isGray(rgb.r,rgb.g,rgb.b)) return false;   // 회색=매진 제외
            if(rgb.r>245&&rgb.g>245&&rgb.b>245) return false; // 흰색 제외
            if(!target) return true;                       // 모두: 색 있는 좌석
            // 선택한 등급 색상과의 거리
            var d=Math.abs(rgb.r-target[0])+Math.abs(rgb.g-target[1])+
                  Math.abs(rgb.b-target[2]);
            return d<=70;
        }
        function clsStr(el){
            var c=(el.className&&el.className.baseVal!==undefined)
                  ?el.className.baseVal:(el.className||'');
            return (''+c).toLowerCase();
        }
        function isSoldByClass(el){
            var c=clsStr(el);
            return c.indexOf('sold')>=0||c.indexOf('disable')>=0||
                   c.indexOf('reserved')>=0||c.indexOf('unavailab')>=0;
        }
        function isLegend(el){
            // 범례 스와치(cls=lv 등) 제외 — 좌석 아님
            var c=clsStr(el);
            return c==='lv'||c.indexOf('legend')>=0||c.indexOf('grade')>=0;
        }
        var seats = document.querySelectorAll(
            'rect[fill], circle[fill], path[fill], rect[class], circle[class],' +
            'td, td[bgcolor], td[style], div[style*="background"],' +
            'span[class], img, rect, circle');
        var cands = [];
        for(var i=0;i<seats.length;i++){
            var el=seats[i];
            if(el.getAttribute && el.getAttribute('aria-disabled')==='true') continue;
            if(isSoldByClass(el) || isLegend(el)) continue;
            var bnd=el.getBoundingClientRect?el.getBoundingClientRect():null;
            if(!bnd||bnd.width<3||bnd.height<3) continue;
            if(!matchColor(getRGB(el))) continue;
            cands.push(el);
        }
        if(cands.length===0) return 0;
        // 같은 색 좌석 중 무작위 선택
        var picked=cands[Math.floor(Math.random()*cands.length)];
        function clickable(el){
            for(var d=0; d<4 && el; d++){
                if((el.tagName||'').toLowerCase()==='a'||
                   (el.tagName||'').toLowerCase()==='td'||el.onclick||
                   (el.getAttribute&&el.getAttribute('onclick'))) return el;
                el=el.parentElement;
            }
            return null;
        }
        // onclick 가진 요소 우선, 토글이므로 '딱 한 번만' 실행
        var tgt = (typeof picked.onclick==='function') ? picked
                  : (clickable(picked) || picked);
        try{
            if(typeof tgt.onclick==='function'){ tgt.onclick.call(tgt); }
            else if(tgt.click){ tgt.click(); }
            else { tgt.dispatchEvent(new MouseEvent('click',
                {bubbles:true,cancelable:true,view:window})); }
        }catch(e){}
        return cands.length;
        """
        # 등급명(title/alt) 기반 탐지용 JS - 색이 안 잡히는 이미지 좌석 대응
        gname = (grade or {}).get("name", "")
        attr_js = r"""
        var gname = (arguments[0]||'').replace(/\s+/g,'');
        function clsOf(el){var c=el.className;if(c&&c.baseVal!==undefined)c=c.baseVal;return(''+(c||'')).toLowerCase();}
        function isSold(el){
            var c=clsOf(el);
            var t=((el.getAttribute('title')||'')+(el.getAttribute('alt')||'')).toLowerCase();
            var src=((el.getAttribute('src')||'')).toLowerCase();
            return c.indexOf('sold')>=0||c.indexOf('disable')>=0||c.indexOf('reserved')>=0
                 ||c.indexOf('unavailab')>=0||t.indexOf('판매완료')>=0
                 ||src.indexOf('sold')>=0||src.indexOf('disable')>=0||src.indexOf('_n')>=0;
        }
        var nodes=document.querySelectorAll('[title],[alt]');
        var cands=[];
        for(var i=0;i<nodes.length;i++){
            var el=nodes[i];
            var t=((el.getAttribute('title')||'')+(el.getAttribute('alt')||'')).replace(/\s+/g,'');
            if(gname && t.indexOf(gname)<0) continue;     // 등급명 포함 좌석만
            var v=(el.getAttribute('value')||'');
            if(v && v!=='N') continue;                    // value 있으면 N(여석)만
            if(isSold(el)) continue;
            var b=el.getBoundingClientRect?el.getBoundingClientRect():null;
            if(!b||b.width<2||b.height<2) continue;
            cands.push(el);
        }
        if(cands.length===0) return 0;
        var p=cands[Math.floor(Math.random()*cands.length)];
        try{
            if(typeof p.onclick==='function'){ p.onclick.call(p); }
            else if(p.click){ p.click(); }
            else { p.dispatchEvent(new MouseEvent('click',
                {bubbles:true,cancelable:true,view:window})); }
        }catch(e){}
        return cands.length;
        """

        # 0) 좌석 title 의 등급명으로 매칭 (가장 정확) + onclick(SelectSeat) 직접 실행
        # 좌석: <span class="SeatN" title="[지정석 R석] ..." onclick="SelectSeat(this,...)">
        title_js = r"""
        var gname=(arguments[0]||'').replace(/\s+/g,'');
        var nodes=document.querySelectorAll("[onclick*='SelectSeat']");
        var cands=[];
        for(var i=0;i<nodes.length;i++){
            var el=nodes[i];
            var title=(el.getAttribute('title')||'').replace(/\s+/g,'');
            if(gname && title.indexOf(gname)<0) continue;   // 선택 등급 좌석만
            var b=el.getBoundingClientRect?el.getBoundingClientRect():null;
            if(!b||b.width<2||b.height<2) continue;
            cands.push(el);
        }
        if(cands.length===0) return 0;
        var p=cands[Math.floor(Math.random()*cands.length)];   // 같은 등급 중 랜덤
        // 좌석 선택은 토글이므로 '딱 한 번만' 실행 (여러번이면 도로 해제됨)
        try{
            if(typeof p.onclick==='function'){ p.onclick.call(p); }
            else if(p.click){ p.click(); }
            else { p.dispatchEvent(new MouseEvent('click',
                {bubbles:true,cancelable:true,view:window})); }
        }catch(e){}
        return cands.length;
        """

        # 1) title(등급명) 기반 탐지 (우선)
        try:
            drv.switch_to.default_content()
        except Exception:
            pass
        try:
            n = self._click_seat_recursive(title_js, gname, 0)
        except Exception:
            n = 0
        self._accept_alert()
        # 2) 못 찾으면 색 기반 탐지
        if not n:
            try: drv.switch_to.default_content()
            except Exception: pass
            try:
                n = self._click_seat_recursive(js, target, 0)
            except Exception:
                n = 0
            self._accept_alert()
        # 3) 그래도 못 찾으면 등급명 속성(title/alt) 기반
        if not n:
            try: drv.switch_to.default_content()
            except Exception: pass
            try:
                n = self._click_seat_recursive(attr_js, gname, 0)
            except Exception:
                n = 0
            self._accept_alert()
        if n and n > 0:
            disp = gname or "모두"
            self.log(f"[{disp}] 빈 좌석 클릭 시도 (후보 {n}개)")
            # 선택 반영이 늦을 수 있어 최대 ~4초간 '총 N석'을 폴링
            for _ in range(8):
                self._accept_alert()
                if self._seat_selected() is True:
                    self.log("→ 좌석 선택됨")
                    return True
                self._wait(0.5)
            self.log("→ 아직 선택 안 됨(총 0석), 계속 탐색")
            return False
        return False

    # 알림창(alert/confirm)이 뜨면 수락. (좌석 클릭 후 안내창 처리)
    def _accept_alert(self):
        try:
            alert = self.driver.switch_to.alert
            txt = ""
            try: txt = alert.text
            except Exception: pass
            alert.accept()
            if txt:
                self.log(f"[알림] {txt[:50]}")
            return True
        except Exception:
            return False

    # 선택좌석 패널의 '총 N석' 값으로 실제 선택 여부 확인.
    # True=선택됨, False=0석(미선택), None=확인불가
    def _seat_selected(self):
        def fn():
            try:
                t = (self.driver.execute_script(
                    "return document.body?document.body.innerText:''") or "").replace(' ', '')
                # '총 N석 선택' 카운터를 우선 매칭, 없으면 모든 '총 N석'
                nums = [int(x) for x in re.findall(r'총(\d+)석선택', t)]
                if not nums:
                    nums = [int(x) for x in re.findall(r'총(\d+)석', t)]
                return nums
            except Exception:
                return []
        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        vals = self._collect_all_frames(fn)
        try: drv.switch_to.default_content()
        except Exception: pass
        if vals:
            return max(vals) > 0   # 어느 프레임이든 총 N석>0 이면 선택된 것
        return None

    # 현재 프레임이 좌석배치도(상세) 프레임인지 (메인 패널의 범례 오클릭 방지)
    def _is_seat_detail_frame(self):
        try:
            t = self.driver.execute_script(
                "return document.body?document.body.innerText:''") or ""
        except Exception:
            return False
        return ('좌석배치도' in t) or ('입장번호' in t) or bool(re.search(r'\d+\s*열', t))

    # 좌석배치도 프레임에서만 좌석 클릭 시도. (메인의 등급 범례 보라네모 오클릭 방지)
    def _click_seat_recursive(self, js, target, depth):
        drv = self.driver
        # 좌석상세 프레임일 때만 좌석 탐지/클릭 실행
        if self._is_seat_detail_frame():
            try:
                n = drv.execute_script(js, target)
            except Exception:
                n = 0
            if n and n > 0:
                return n
        if depth >= 4:
            return 0
        try:
            cnt = len(drv.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            cnt = 0
        for i in range(cnt):
            try:
                frames = drv.find_elements(By.TAG_NAME, "iframe")
                if i >= len(frames):
                    break
                drv.switch_to.frame(frames[i])
                n = self._click_seat_recursive(js, target, depth + 1)
                if n and n > 0:
                    return n
                drv.switch_to.parent_frame()
            except Exception:
                try: drv.switch_to.parent_frame()
                except Exception:
                    try: drv.switch_to.default_content()
                    except Exception: pass
        return 0

    # ── 퍼즐 슬라이더 ─────────────────────────
    def _solve_puzzle(self):
        drv = self.driver
        # 실제로 '크기가 있는' 퍼즐 컨테이너가 보일 때만 진행 (오탐/에러 스팸 방지)
        found = False
        for sel in [".slider_wrap", ".puzzle_wrap",
                    "[class*='slider']", "[class*='puzzle']"]:
            try:
                el = drv.find_element(By.CSS_SELECTOR, sel)
                sz = el.size
                if el.is_displayed() and sz.get('width', 0) > 5 and sz.get('height', 0) > 5:
                    found = True; break
            except Exception:
                pass
        if not found:
            return

        offset, bg_path, pc_path = 140, "puzzle_bg.png", "puzzle_pc.png"
        bg_el = pc_el = None
        for s in ["#captcha_bg","#puzzle_bg","img[id*='bg']",".puzzle_bg"]:
            try: bg_el = drv.find_element(By.CSS_SELECTOR, s); break
            except: pass
        for s in ["#captcha_piece","#puzzle_piece","img[id*='piece']",".puzzle_piece"]:
            try: pc_el = drv.find_element(By.CSS_SELECTOR, s); break
            except: pass
        if bg_el and pc_el:
            try:
                bg_el.screenshot(bg_path); pc_el.screenshot(pc_path)
                bg = np.array(Image.open(bg_path).convert("L"), dtype=np.float32)
                pc = np.array(Image.open(pc_path).convert("L"), dtype=np.float32)
                ph, pw = pc.shape; bh, bw = bg.shape
                best_x, best_sc = 0, float("inf")
                for x in range(0, bw - pw, 2):
                    sc = float(np.mean(np.abs(bg[:ph, x:x+pw] - pc)))
                    if sc < best_sc: best_sc = sc; best_x = x
                offset = best_x
            except: pass
        self.log(f"[퍼즐 감지] 목표 위치: {offset}px")
        slider = None
        for s in [".btn_slide_right",".slide_btn",".slider_btn",
                  "div[class*='slider'] span","#nc_1__scale_text"]:
            try:
                el = drv.find_element(By.CSS_SELECTOR, s)
                sz = el.size
                if el.is_displayed() and sz.get('width', 0) > 0 and sz.get('height', 0) > 0:
                    slider = el; break
            except Exception:
                pass
        if slider:
            try:
                ac = ActionChains(drv)
                ac.click_and_hold(slider).pause(0.3)
                for _ in range(20): ac.move_by_offset(offset/20, 0).pause(0.02)
                ac.release().perform(); self._wait(1.2)
            except Exception:
                pass   # 슬라이더 드래그 실패는 조용히 무시 (없는 경우가 대부분)

    # ── 구역 클릭 ─────────────────────────────
    # zone: {'label','href',...} 또는 라벨 문자열.
    # 이미지맵은 href(자바스크립트) 실행이 가장 확실하므로 모든 프레임을
    # 재귀로 훑어 해당 area 의 href 를 실행한다. 없으면 라벨/JS로 폴백.
    def _click_zone(self, zone):
        if isinstance(zone, dict):
            label = zone.get('label', '')
            href  = zone.get('href', '') or ''
        else:
            label, href = str(zone), ''
        zone_num = label.replace("구역", "").replace("영역", "").strip()
        if "(" in zone_num:
            zone_num = zone_num.split("(")[-1].replace(")", "").strip()

        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        if self._click_zone_recursive(zone_num, href, 0):
            return True
        try: drv.switch_to.default_content()
        except Exception: pass
        return False

    # 모든 프레임을 재귀로 훑어 구역(area) 클릭 시도. 성공 시 그 프레임에 머무름.
    def _click_zone_recursive(self, zone_num, href, depth):
        drv = self.driver
        if self._click_zone_in_frame(zone_num, href):
            return True
        if depth >= 4:
            return False
        try:
            cnt = len(drv.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            cnt = 0
        for i in range(cnt):
            try:
                frames = drv.find_elements(By.TAG_NAME, "iframe")
                if i >= len(frames):
                    break
                drv.switch_to.frame(frames[i])
                if self._click_zone_recursive(zone_num, href, depth + 1):
                    return True
                drv.switch_to.parent_frame()
            except Exception:
                try: drv.switch_to.parent_frame()
                except Exception:
                    try: drv.switch_to.default_content()
                    except Exception: pass
        return False

    # 현재 프레임 안에서만 구역 클릭 시도 (프레임 전환 없음)
    def _click_zone_in_frame(self, zone_num, href):
        drv = self.driver
        # 0) 패널 링크(a[href]) 가 보관한 href 와 일치하면 그 요소를 마우스 클릭
        if href:
            try:
                for el in drv.find_elements(By.CSS_SELECTOR, "a[href]"):
                    if (el.get_attribute("href") or "") == href:
                        drv.execute_script(
                            "var e=arguments[0];"
                            "['mouseover','mousedown','mouseup','click'].forEach("
                            "function(t){e.dispatchEvent(new MouseEvent(t,"
                            "{bubbles:true,cancelable:true,view:window}));});"
                            "if(e.click)e.click();", el)
                        return True
            except Exception:
                pass
            if href.startswith("javascript:"):
                try:
                    drv.execute_script(href[len("javascript:"):])
                    return True
                except Exception:
                    pass
        # 1) 보관해 둔 href 와 일치하는 area 실행 (가장 정확)
        if href:
            try:
                for area in drv.find_elements(By.TAG_NAME, "area"):
                    if (area.get_attribute("href") or "") == href:
                        if href.startswith("javascript:"):
                            drv.execute_script(href[len("javascript:"):])
                        else:
                            drv.execute_script("arguments[0].click();", area)
                        return True
            except Exception:
                pass
        # 2) area 의 title/alt/href 에 구역번호가 들어있으면 실행
        try:
            for area in drv.find_elements(By.TAG_NAME, "area"):
                t = (area.get_attribute("title") or
                     area.get_attribute("alt") or "").strip()
                ah = area.get_attribute("href") or ""
                if (t == zone_num or t == zone_num + "구역" or t == zone_num + "영역"
                        or (zone_num and zone_num in ah)):
                    if ah.startswith("javascript:"):
                        drv.execute_script(ah[len("javascript:"):])
                    else:
                        drv.execute_script("arguments[0].click();", area)
                    return True
        except Exception:
            pass
        # 3) SPA/일반 요소 - JS 텍스트/속성 검색
        js = r"""
        var target = arguments[0];
        function tryClick(el){
            for (var d=0; d<6 && el; d++){
                var tag = (el.tagName||'').toLowerCase();
                if (tag==='a' || tag==='button' || el.onclick ||
                    el.getAttribute('role')==='button' ||
                    (el.style && el.style.cursor==='pointer')){
                    el.click(); return true;
                }
                el = el.parentElement;
            }
            return false;
        }
        var nodes = document.querySelectorAll('text,tspan,a,g,span,div,li,button,path');
        for (var i=0;i<nodes.length;i++){
            var txt=(nodes[i].textContent||'').trim();
            if(txt===target||txt===target+'구역'||txt===target+'영역'){
                if(tryClick(nodes[i])) return true;
            }
        }
        var attrs=document.querySelectorAll('[title],[data-zone],[data-area],[aria-label]');
        for(var i=0;i<attrs.length;i++){
            var t=(attrs[i].getAttribute('title')||attrs[i].getAttribute('data-zone')||
                   attrs[i].getAttribute('data-area')||attrs[i].getAttribute('aria-label')||'');
            if(t.trim()===target||t===target+'구역'||t===target+'영역'){
                if(tryClick(attrs[i])) return true;
            }
        }
        return false;
        """
        try:
            return bool(drv.execute_script(js, zone_num))
        except:
            return False

    # ── 구역 순회 ─────────────────────────────
    def _rotate_zones(self, zones, grade=None):
        cycle = 0
        consecutive_err = 0
        self._diag_done = False
        self.log(f"빈 좌석 탐색을 시작합니다 (딜레이 {self.delay}초)")
        while True:
            for zone in zones:
                self._wait(0)
                # 브라우저 세션 생존 확인
                try:
                    _ = self.driver.current_url
                except Exception:
                    self.log("브라우저가 종료되어 순회를 중단합니다."); return

                # 안전망: 이미 좌석이 선택돼 있으면 순회 종료(→ 좌석선택완료 단계로)
                if self._seat_selected() is True:
                    self.log("→ 좌석 선택 확인, 순회 종료")
                    return

                try:
                    # 구역 클릭 (실패해도 다음 구역으로). zone 은 dict 또는 문자열.
                    if not self._click_zone(zone):
                        continue

                    self._wait(self.delay)
                    self._solve_puzzle()

                    # 예매 가능 좌석 클릭 → 성공하면 순회 종료
                    if self._click_seat(grade):
                        return
                    # 첫 구역 진입 후에도 못 잡으면 좌석 DOM 구조를 1회 진단 출력
                    if not self._diag_done:
                        self._diag_done = True
                        self._diagnose_seats()
                    consecutive_err = 0

                except InterruptedError:
                    raise
                except Exception as e:
                    consecutive_err += 1
                    self.log(f"구역 오류(건너뜀): {str(e)[:60]}")
                    if consecutive_err >= 15:
                        self.log("오류가 계속되어 순회를 중단합니다."); return
                    self._wait(0.5)
            cycle += 1
            if cycle % 5 == 0:
                self.log(f"빈 좌석 탐색 중... ({cycle}바퀴째, 아직 못 찾음)")

    # ── 좌석 DOM 진단 (구조 파악용, 1회) ───────
    # 좌석을 못 잡을 때 실제 좌석 요소 샘플을 로그로 출력한다.
    def _diagnose_seats(self):
        # 좌석상세 프레임에서 '좌석처럼 보이는' 요소의 outerHTML 을 덤프해 구조 확인.
        # 좌석은 보통 onclick 이 있거나 background-image(스프라이트)로 색을 입힌다.
        js = r"""
        var out=[];
        var body=document.body?document.body.innerText:'';
        var isDetail = body.indexOf('좌석배치도')>=0 || body.indexOf('입장번호')>=0;
        if(!isDetail) return [];
        var nodes=document.querySelectorAll('img,td,span,div,a,area');
        for(var i=0;i<nodes.length && out.length<8;i++){
            var el=nodes[i];
            var oc=el.getAttribute('onclick')||'';
            var cs=getComputedStyle(el);
            var bgimg=cs.backgroundImage||'';
            var hasBg=(bgimg && bgimg!=='none');
            // 좌석 후보: onclick 있거나 배경이미지로 칠해진 작은 요소
            if(!oc && !hasBg) continue;
            var b=el.getBoundingClientRect?el.getBoundingClientRect():{width:0,height:0};
            if(b.width<2||b.width>60||b.height<2||b.height>60) continue;
            var html=(el.outerHTML||'').replace(/\s+/g,' ').slice(0,160);
            var bgi=bgimg.replace(/.*\//,'').slice(0,30);
            out.push(html+'  |bgimg='+bgi);
        }
        return out;
        """
        def run():
            try: return self.driver.execute_script(js) or []
            except Exception: return []
        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        rows = self._collect_all_frames(run)
        try: drv.switch_to.default_content()
        except Exception: pass
        if rows:
            self.log("[진단] 좌석 요소 outerHTML 샘플:")
            for r in rows[:6]:
                self.log("  " + str(r)[:170])
        else:
            self.log("[진단] 좌석 후보(onclick/배경이미지) 요소를 못 찾음")

    # ── 좌석선택완료 / 티켓가격선택 버튼 클릭 ───
    # 버튼은 보통 메인 예매 페이지(우측 패널)에 있으므로 default content 부터
    # 모든 프레임을 재귀로 훑어 클릭한다.
    def _click_complete(self):
        kws = ['좌석선택완료', '티켓가격선택', '가격선택', '선택완료']
        self.log("좌석선택완료 버튼 찾는 중...")
        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        hit = self._click_complete_recursive(kws, 0)
        if hit:
            self.log(f"→ [{hit}] 클릭 완료")
        else:
            self.log("→ 좌석선택완료 버튼을 찾지 못했습니다")
        self._wait(2.0)

    def _click_complete_recursive(self, kws, depth):
        drv = self.driver
        hit = self._click_complete_in_frame(kws)
        if hit:
            return hit
        if depth >= 4:
            return None
        try:
            cnt = len(drv.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            cnt = 0
        for i in range(cnt):
            try:
                frames = drv.find_elements(By.TAG_NAME, "iframe")
                if i >= len(frames):
                    break
                drv.switch_to.frame(frames[i])
                hit = self._click_complete_recursive(kws, depth + 1)
                if hit:
                    return hit
                drv.switch_to.parent_frame()
            except Exception:
                try: drv.switch_to.parent_frame()
                except Exception:
                    try: drv.switch_to.default_content()
                    except Exception: pass
        return None

    def _click_complete_in_frame(self, kws):
        js = r"""
        var kws = arguments[0];
        function fire(el,t){el.dispatchEvent(new MouseEvent(t,
            {bubbles:true,cancelable:true,view:window}));}
        function clickable(el){
            // 클릭 가능한 조상(a/button/onclick) 찾기
            for(var d=0; d<5 && el; d++){
                var tag=(el.tagName||'').toLowerCase();
                if(tag==='a'||tag==='button'||el.onclick||
                   (el.getAttribute&&el.getAttribute('onclick'))||
                   el.getAttribute('role')==='button') return el;
                el=el.parentElement;
            }
            return null;
        }
        var nodes = document.querySelectorAll('button,a,div,span,li,input,img,td');
        for(var k=0;k<kws.length;k++){
            var kw=kws[k].replace(/\s+/g,'');
            for(var i=0;i<nodes.length;i++){
                var el=nodes[i];
                var t=(el.textContent||'').replace(/\s+/g,'').trim();
                var v=((el.value||'')+'').replace(/\s+/g,'').trim();
                var a=((el.getAttribute&&(el.getAttribute('alt')||el.getAttribute('title')))||'').replace(/\s+/g,'').trim();
                if(t.indexOf(kw)>=0 || v.indexOf(kw)>=0 || a.indexOf(kw)>=0){
                    var b=el.getBoundingClientRect();
                    if(b.width>0 && b.height>0){
                        var tgt=clickable(el)||el;
                        try{
                            fire(tgt,'mouseover'); fire(tgt,'mousedown');
                            fire(tgt,'mouseup'); fire(tgt,'click');
                            if(tgt.click) tgt.click();
                        }catch(e){
                            el.dispatchEvent(new MouseEvent('click',
                                {bubbles:true,cancelable:true,view:window}));
                        }
                        return kws[k];
                    }
                }
            }
        }
        return null;
        """
        try:
            return self.driver.execute_script(js, kws)
        except Exception:
            return None

        # 3) 클릭 후 페이지가 넘어갔는지 확인, 아직 좌석화면이면 재시도
    # ── 결제 페이지 감지 ──────────────────────
    # motickets: step3 이상 URL 또는 결제 전용 페이지로 이동했을 때만 감지
    def _is_payment_page(self):
        try:
            url = self.driver.current_url
            # motickets 결제 단계: step3, payment, checkout, order 등
            pay_url_kw = ["step3", "payment", "checkout", "order", "BookEnd",
                          "poticket", "pay/"]
            if any(k in url for k in pay_url_kw):
                return True
            # step2는 절대 결제 페이지 아님
            if "step2" in url:
                return False
            # step2 아닌 다른 URL로 이동했고 결제 키워드가 소스에 있을 때
            src = self.driver.page_source
            pay_src_kw = ["주문금액", "결제수단", "최종결제금액", "결제하기"]
            return any(k in src for k in pay_src_kw)
        except:
            return False

    # ── 결제 대기 ─────────────────────────────
    def _wait_payment(self):
        self.log("취소표를 잡았습니다! 결제 페이지 대기 중 - 소리 알림 시작")

        def _beep_loop(stop_ev):
            while not stop_ev.is_set():
                try:
                    winsound.Beep(1000, 400)
                    time.sleep(0.15)
                    winsound.Beep(1300, 400)
                    time.sleep(0.5)
                except:
                    break

        stop_ev = threading.Event()
        threading.Thread(target=_beep_loop, args=(stop_ev,), daemon=True).start()

        try:
            # 최대 30분 대기
            for _ in range(1800):
                self._wait(1)
                try:
                    url = self.driver.current_url
                    src = self.driver.page_source
                    done_kw = ["BookEnd", "결제완료", "예매완료", "주문완료",
                               "step3", "payment/complete"]
                    if any(k in url for k in done_kw):
                        self.log("✅ 결제 완료!")
                        stop_ev.set(); return
                except:
                    pass
            self.log("결제 대기 시간 초과 (30분)")
        finally:
            stop_ev.set()

    # ── 메인 흐름 ─────────────────────────────
    def run(self):
        try:
            # ① 로그인 대기
            self.log("매크로 실행 시작")
            self.log("로그인을 완료해 주세요.")
            for _ in range(300):
                self._wait(1)
                if self._is_logged_in(): break
            else:
                self.log("로그인 대기 시간 초과"); return
            self.log("→ 로그인 완료")
            # 로그인 후 자동 리다이렉트가 끝나길 잠깐 대기 (조기 이동 방지)
            self._wait(2.0)

            # 로그인 직후 야놀자 도메인에 있으면 놀 인터파크 메인으로 "한 번만" 이동.
            # 단, 이미 인터파크 도메인(메인/예매창 등)에 있으면 끌고 가지 않는다.
            if "interpark.com" not in self._url():
                try:
                    self.log("→ 놀 인터파크 메인(nol.interpark.com)으로 이동")
                    self.driver.get("https://nol.interpark.com/")
                    self._wait(2.0)
                except:
                    self._wait(1)

            # ② 예매 페이지 대기 (예매창은 "새 창"으로 열린다)
            # 매초 창을 번갈아 전환하면 두 창이 계속 새로고침되므로,
            # 창 개수가 늘어났을 때(=새 예매창 등장)만 그 창으로 "한 번" 전환한다.
            self.log("원하시는 링크에 들어가서 [예매하기] 버튼을 눌러 주세요.")
            booking_kw = ("poticket", "Book")  # NOL 인터파크 예매창 = poticket
            try:
                known = len(self.driver.window_handles)
            except Exception:
                known = 1
            for _ in range(1800):
                self._wait(1)
                # 현재 창이 예매 페이지면 완료 (도달 시 딱 한 번만 로그)
                try:
                    if any(k in self.driver.current_url for k in booking_kw):
                        self.log("→ 예매창으로 전환 완료")
                        break
                except Exception:
                    pass
                # 새 창이 열리면(개수 증가) 가장 최근 창으로 조용히 전환
                try:
                    handles = self.driver.window_handles
                    if len(handles) > known:
                        known = len(handles)
                        self.driver.switch_to.window(handles[-1])
                        self._wait(1.5)
                except Exception:
                    pass
            self._wait(2)

            # ③ 안심예매 보안문자: 예매창에서 직접 입력 → 통과 대기
            if self._captcha_present():
                self._wait_captcha_cleared()
                self._wait(1)

            # ④ 좌석 등급 선택 (페이지에서 동적으로 읽음)
            grade, grade_list = self._ask_grade()
            self._wait(0.3)

            # ⑤ 구역 선택 (zones = 구역 dict 리스트)
            zones = self._ask_zones(grade, grade_list)
            zone_labels = [z.get('label', '') for z in zones] if zones else []
            self.log(f"선택 구역: {', '.join(zone_labels) if zone_labels else '전체'} "
                     f"({len(zones)}개)")
            self._wait(0.3)

            # ⑥ 구역 순회 + 좌석 클릭
            self._rotate_zones(zones, grade)

            # ⑦ 좌석선택완료
            self._click_complete()

            # ⑦-② 결제 페이지 진입 대기 (가격/할인선택 단계)
            self.log("결제 페이지 진입 대기 중...")
            for _ in range(60):
                self._wait(1)
                if self._is_payment_page():
                    break

            # ⑧ 결제 대기 + 소리 알림
            self._wait_payment()

        except InterruptedError:
            self.log("매크로 중단됨")
        except Exception as e:
            self.log(f"오류: {e}\n{traceback.format_exc()}")


# ───────────────────────────────────────────────
#  컨트롤 창
# ───────────────────────────────────────────────
class ControlWindow(QWidget):
    def __init__(self, driver, delay):
        super().__init__()
        self.setWindowTitle("NOL 인터파크 취소표 매크로")
        self.setFont(QFont("맑은 고딕", 9))
        self.setFixedWidth(310)

        self._sig    = Signals()
        self._thread = MacroThread(driver, delay, self._sig)
        self._paused = False

        self._sig.log_signal.connect(self._on_log)
        self._sig.show_input.connect(self._show_input)
        self._sig.hide_input.connect(self._hide_input)

        root = QVBoxLayout()
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # 로그 박스
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFixedHeight(130)
        self.log_box.setFont(QFont("맑은 고딕", 9))
        self.log_box.setStyleSheet(
            "background:#ffffff; color:#222; border:1px solid #ccc;"
        )
        root.addWidget(self.log_box)

        # 입력 행 (기본 숨김)
        self.input_row = QWidget()
        rl = QHBoxLayout()
        rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(4)
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("보안문자 / 번호 입력")
        self.input_edit.setFixedHeight(28)
        self.input_edit.returnPressed.connect(self._submit)
        self.input_btn = QPushButton("입력완료")
        self.input_btn.setFixedSize(64, 28)
        self.input_btn.setStyleSheet(
            "background:#4a90d9; color:white; font-weight:bold; border-radius:3px;"
        )
        self.input_btn.clicked.connect(self._submit)
        rl.addWidget(self.input_edit); rl.addWidget(self.input_btn)
        self.input_row.setLayout(rl)
        self.input_row.hide()
        root.addWidget(self.input_row)

        # 중단/일시정지 버튼
        bl = QHBoxLayout(); bl.setSpacing(6)
        self.btn_stop = QPushButton("중단하기")
        self.btn_stop.setFixedHeight(32)
        self.btn_stop.setStyleSheet(
            "background:#4a4a4a; color:white; font-weight:bold; border-radius:4px;"
        )
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_pause = QPushButton("일시정지")
        self.btn_pause.setFixedHeight(32)
        self.btn_pause.setStyleSheet(
            "background:#3db06e; color:white; font-weight:bold; border-radius:4px;"
        )
        self.btn_pause.clicked.connect(self._on_pause)
        bl.addWidget(self.btn_stop); bl.addWidget(self.btn_pause)
        root.addLayout(bl)

        self.setLayout(root)
        self.adjustSize()
        self._thread.start()

    def _on_log(self, msg):
        self.log_box.append(msg)
        sb = self.log_box.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _show_input(self):
        self.input_edit.clear()
        self.input_row.show()
        self.input_edit.setFocus()
        self.adjustSize()

    def _hide_input(self):
        self.input_row.hide()
        self.adjustSize()

    def _submit(self):
        self._thread.set_answer(self.input_edit.text().strip())

    def _on_stop(self):
        self._thread.stop()
        self.btn_stop.setEnabled(False)
        self.btn_pause.setEnabled(False)

    def _on_pause(self):
        self._paused = self._thread.toggle_pause()
        if self._paused:
            self.btn_pause.setText("재  개")
            self.btn_pause.setStyleSheet(
                "background:#d0a020; color:white; font-weight:bold; border-radius:4px;"
            )
        else:
            self.btn_pause.setText("일시정지")
            self.btn_pause.setStyleSheet(
                "background:#3db06e; color:white; font-weight:bold; border-radius:4px;"
            )

    def closeEvent(self, event):
        self._thread.stop()
        self._thread.wait(2000)
        event.accept()


# ───────────────────────────────────────────────
#  로그인 창
# ───────────────────────────────────────────────
class LoginWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NOL 인터파크 취소표 매크로")
        self.setFixedWidth(300)
        self.setFont(QFont("맑은 고딕", 10))
        self._ctrl = None

        root = QVBoxLayout()
        root.setSpacing(8)
        root.setContentsMargins(16, 16, 16, 16)

        root.addWidget(QLabel("로그인"))

        # 인터파크 / 카카오 / 네이버 탭
        tab_row = QHBoxLayout(); tab_row.setSpacing(0)
        self._tab_btns = [
            QPushButton("인터파크"),
            QPushButton("카카오"),
            QPushButton("네이버"),
        ]
        self._tab_grp = QButtonGroup(self)
        for i, b in enumerate(self._tab_btns):
            b.setCheckable(True); b.setFixedHeight(30)
            self._tab_grp.addButton(b, i); tab_row.addWidget(b)
        self._tab_btns[0].setChecked(True)
        self._tab_grp.buttonClicked.connect(lambda _: self._update_tabs())
        self._update_tabs()
        root.addLayout(tab_row)

        # ID
        r = QHBoxLayout()
        lb = QLabel("ID"); lb.setFixedWidth(40)
        self.edit_id = QLineEdit(); self.edit_id.setFixedHeight(28)
        r.addWidget(lb); r.addWidget(self.edit_id); root.addLayout(r)

        # PW
        r = QHBoxLayout()
        lb = QLabel("PW"); lb.setFixedWidth(40)
        self.edit_pw = QLineEdit()
        self.edit_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_pw.setFixedHeight(28)
        r.addWidget(lb); r.addWidget(self.edit_pw); root.addLayout(r)

        # 딜레이
        r = QHBoxLayout()
        lb = QLabel("딜레이"); lb.setFixedWidth(44)
        self.spin = QSpinBox()
        self.spin.setRange(1, 60); self.spin.setValue(1); self.spin.setFixedHeight(28)
        r.addWidget(lb); r.addWidget(self.spin); r.addStretch(); root.addLayout(r)

        # 유효기간
        lbl = QLabel(f"유효기간: {EXPIRE_DATE}")
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        lbl.setStyleSheet("color:#888; font-size:9px;")
        root.addWidget(lbl)

        # 시작하기
        self.btn_start = QPushButton("시작하기")
        self.btn_start.setFixedHeight(36)
        self.btn_start.setStyleSheet(
            "background:#3c3c3c; color:white; font-weight:bold;"
            " font-size:12px; border-radius:4px;"
        )
        self.btn_start.clicked.connect(self._on_start)
        root.addWidget(self.btn_start)

        self.setLayout(root)

    def _update_tabs(self):
        for b in self._tab_btns:
            b.setStyleSheet(
                "background:#4a90d9; color:white; font-weight:bold;"
                " border:none; border-radius:3px;" if b.isChecked() else
                "background:#e0e0e0; color:#333; border:none; border-radius:3px;"
            )

    def _on_start(self):
        delay = self.spin.value()
        try:
            opts = webdriver.ChromeOptions()
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            opts.add_experimental_option("useAutomationExtension", False)
            drv = webdriver.Chrome(
                service=Service(ChromeDriverManager().install()), options=opts
            )
            drv.set_page_load_timeout(30)
            drv.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"}
            )
        except Exception as e:
            QMessageBox.critical(self, "오류", f"Chrome 실행 실패:\n{e}"); return

        try: drv.get(LOGIN_URL)
        except: pass

        self._ctrl = ControlWindow(drv, delay)
        self._ctrl.show()
        self.hide()


# ───────────────────────────────────────────────
#  진입점
# ───────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("맑은 고딕", 10))
    win = LoginWindow()
    win.show()
    sys.exit(app.exec())
