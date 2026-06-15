# -*- coding: utf-8 -*-

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
        end = time.time() + sec
        while time.time() < end:
            if self._stop: raise InterruptedError
            while self._pause:
                if self._stop: raise InterruptedError
                time.sleep(0.1)
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

    # ── 캡챠 입력창 요소 찾기 (iframe 포함) ──
    # 캡챠 전용 placeholder만 엄격하게 매칭 (다른 입력창 오인 방지)
    def _find_captcha_input(self):
        drv = self.driver
        input_sels = [
            "input[placeholder*='문자를 입력해주세요']",
            ".captcha_input input",
            "#captchaInput",
        ]
        # 1) 현재 컨텍스트에서 탐색
        for sel in input_sels:
            try:
                el = drv.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed(): return el
            except: pass
        # 2) 모든 iframe 시도
        try:
            frames = drv.find_elements(By.TAG_NAME, "iframe")
        except: frames = []
        for frame in frames:
            try:
                drv.switch_to.frame(frame)
                for sel in input_sels:
                    try:
                        el = drv.find_element(By.CSS_SELECTOR, sel)
                        if el.is_displayed(): return el
                    except: pass
                drv.switch_to.default_content()
            except:
                drv.switch_to.default_content()
        return None

    # ── 안심예매 캡챠 팝업 감지 ────────────────
    # 캡챠 전용 입력창(문자를 입력해주세요)이 실제로 화면에 보일 때만 True
    def _captcha_visible(self):
        drv = self.driver
        try:
            drv.switch_to.default_content()
            return self._find_captcha_input() is not None
        except:
            return False

    # ── 안심예매 캡챠 처리 ────────────────────
    def _handle_captcha(self):
        drv = self.driver

        for attempt in range(10):
            drv.switch_to.default_content()
            if not self._captcha_visible():
                self.log("→ 캡챠 통과"); return True

            self.log("보안 문자를 입력해주세요. 없을 경우, 0을 입력해주세요 →")
            ans = self._ask(timeout=90)
            if ans is None:
                self.log("입력 시간 초과"); return False
            self.log(f"→ {ans}")

            if ans.strip() == "0":
                return True

            try:
                drv.switch_to.default_content()
                inp = self._find_captcha_input()
                if not inp:
                    self.log("입력창을 찾지 못했습니다"); return False

                inp.clear()
                inp.send_keys(ans)
                self._wait(0.3)

                # 입력완료 버튼 클릭
                confirmed = False
                for bsel in [
                    "//button[contains(text(),'입력완료')]",
                    "//a[contains(text(),'입력완료')]",
                    "//button[contains(text(),'확인')]",
                    "//input[@type='submit']",
                ]:
                    try:
                        btn = drv.find_element(By.XPATH, bsel)
                        if btn.is_displayed():
                            drv.execute_script("arguments[0].click();", btn)
                            confirmed = True; break
                    except: pass

                if not confirmed:
                    for bsel in [".btn_ok", ".btn_confirm", "button.confirm",
                                 "button[type='submit']"]:
                        try:
                            el = drv.find_element(By.CSS_SELECTOR, bsel)
                            if el.is_displayed():
                                drv.execute_script("arguments[0].click();", el)
                                confirmed = True; break
                        except: pass

                drv.switch_to.default_content()
                self._wait(2.0)  # 제출 후 충분히 대기

            except Exception as e:
                self.log(f"캡챠 처리 오류: {e}")
                drv.switch_to.default_content()
                continue

            drv.switch_to.default_content()
            if not self._captcha_visible():
                self.log("→ 캡챠 통과"); return True

            self.log(f"캡챠 재시도 ({attempt+1}/10)")

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
        drv = self.driver
        for xp in [
            "//*[contains(text(),'좌석가격보기')]",
            "//*[contains(text(),'가격보기')]",
        ]:
            try:
                el = drv.find_element(By.XPATH, xp)
                if el.is_displayed():
                    drv.execute_script("arguments[0].click();", el)
                    self._wait(0.6)
                    return True
            except: pass
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
            var name = txt.replace(/[\d,]+\s*원.*/,'').replace(/\s+/g,' ').trim();
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

    # ── 등급 목록 읽기 ────────────────────────
    # 1) 가격 패널을 열지 않고 DOM 스캔 → 보이지 않게 처리 (문제 1/3)
    # 2) 못 읽으면 폴백으로 패널을 잠깐 열었다 닫고 읽음
    def _get_grades(self):
        rows = self._scan_grades()
        if rows:
            return rows
        # 폴백: 가격 패널을 열어 읽은 뒤 곧바로 닫음
        self._open_price_panel()
        self._wait(0.4)
        rows = self._scan_grades()
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
    def _get_zones(self):
        drv = self.driver
        zones = []
        # 1) BookMain.asp (iframe) - area 태그 title/alt (색상 없음)
        try:
            drv.switch_to.default_content()
            self._to_frame("ifrmSeat", "mainFrame")
            for a in drv.find_elements(By.TAG_NAME, "area"):
                t = (a.get_attribute("title") or a.get_attribute("alt") or "").strip()
                if t: zones.append({'label': t, 'color': None})
            drv.switch_to.default_content()
        except:
            try: drv.switch_to.default_content()
            except: pass
        if zones:
            return self._dedup_zones(zones)
        # 2) motickets (SVG/HTML)
        #    라벨(001,101 등) 텍스트를 먼저 찾고, 그 부모/형제 도형의
        #    fill/배경색을 읽어 등급 색상과 짝지음.
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
        # default content + 모든 iframe 안에서 시도
        zones = self._run_zone_js(js)
        if not zones:
            try:
                frames = drv.find_elements(By.TAG_NAME, "iframe")
            except:
                frames = []
            for fr in frames:
                try:
                    drv.switch_to.default_content()
                    drv.switch_to.frame(fr)
                    zones = self._run_zone_js(js)
                    if zones: break
                except:
                    pass
            try: drv.switch_to.default_content()
            except: pass
        return self._dedup_zones(zones)

    def _run_zone_js(self, js):
        try: return self.driver.execute_script(js) or []
        except: return []

    def _dedup_zones(self, items):
        seen, out = set(), []
        for x in items:
            lbl = x.get('label', '')
            if lbl and lbl not in seen:
                seen.add(lbl); out.append(x)
        return out

    def _dedup(self, items):
        seen, out = set(), []
        for x in items:
            if x not in seen:
                seen.add(x); out.append(x)
        return out

    # ── 구역 선택 (등급 색상으로 필터링) ──────
    def _ask_zones(self, grade=None):
        all_zones = self._get_zones()
        grade_color = (grade or {}).get('color')

        # 등급 색상이 있고, 구역에도 색상 정보가 있으면 필터링
        if grade_color and any(z.get('color') for z in all_zones):
            def _color_match(zc):
                if not zc: return False
                d = abs(zc[0]-grade_color[0]) + abs(zc[1]-grade_color[1]) + abs(zc[2]-grade_color[2])
                return d <= 90
            filtered = [z for z in all_zones if _color_match(z.get('color'))]
            if filtered:
                self.log(f"[등급 필터] {grade['name']} 색상에 맞는 구역 {len(filtered)}개 표시")
                zone_list = filtered
            else:
                self.log("색상 필터링 결과 없음 → 전체 구역 표시")
                zone_list = all_zones
        else:
            zone_list = all_zones

        if not zone_list:
            self.log("구역 목록을 읽지 못했습니다. 직접 입력하세요 (예: 105,106)")
        else:
            for i, z in enumerate(zone_list, 1):
                self.log(f"{i}. {z['label']}")
        self.log("구역을 번호로 입력해주세요. ','로 구분하여 여러개 입력 가능합니다.")
        ans = self._ask(timeout=120)
        labels = [z['label'] for z in zone_list]
        if not ans: return labels
        self.log(f"→ {ans}")
        selected = []
        for part in ans.split(","):
            part = part.strip()
            if not part: continue
            try:
                idx = int(part) - 1
                if 0 <= idx < len(zone_list):
                    selected.append(zone_list[idx]['label'])
                else:
                    selected.append(part)
            except:
                selected.append(part)
        return selected if selected else labels

    # ── 좌석 클릭 ─────────────────────────────
    # grade: {'name','color':[r,g,b]} 또는 None(모두)
    def _click_seat(self, grade=None):
        drv = self.driver
        target = (grade or {}).get("color")  # [r,g,b] 또는 None

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
        function isSoldByClass(el){
            var c=(el.className&&el.className.baseVal!==undefined)
                  ?el.className.baseVal:(el.className||'');
            c=(''+c).toLowerCase();
            return c.indexOf('sold')>=0||c.indexOf('disable')>=0||
                   c.indexOf('reserved')>=0||c.indexOf('unavailab')>=0;
        }
        var seats = document.querySelectorAll(
            'rect[fill], circle[fill], path[fill], rect[class], circle[class],' +
            'td, td[bgcolor], td[style], div[style*="background"],' +
            'rect, circle');
        var cands = [];
        for(var i=0;i<seats.length;i++){
            var el=seats[i];
            if(el.getAttribute && el.getAttribute('aria-disabled')==='true') continue;
            if(isSoldByClass(el)) continue;
            var bnd=el.getBoundingClientRect?el.getBoundingClientRect():null;
            if(!bnd||bnd.width<3||bnd.height<3) continue;
            if(!matchColor(getRGB(el))) continue;
            cands.push(el);
        }
        if(cands.length===0) return 0;
        var picked=cands[0];
        try { picked.click(); } catch(e){
            picked.dispatchEvent(new MouseEvent('click',
                {bubbles:true,cancelable:true,view:window}));
        }
        return cands.length;
        """
        try:
            n = drv.execute_script(js, target)
            if n and n > 0:
                gname = (grade or {}).get("name", "모두")
                self.log(f"[{gname}] 예매 가능 좌석 발견 → 클릭 (후보 {n}개)")
                self._wait(1.2)
                # 하단에 좌석 선택 정보(티켓가격선택/총 N매)가 나타났는지 확인
                if "티켓가격선택" in self._src() or "총" in self._src():
                    self._close_seat_panel()   # 잔여좌석 안내 패널 닫기 (문제 3)
                    return True
                self._wait(0.8)
                if "티켓가격선택" in self._src() or "총" in self._src():
                    self._close_seat_panel()
                    return True
                return False
        except Exception as e:
            self.log(f"좌석 클릭 오류: {str(e)[:80]}")
        return False

    # ── 퍼즐 슬라이더 ─────────────────────────
    def _solve_puzzle(self):
        drv = self.driver
        found = False
        for sel in [".slider_wrap",".puzzle_wrap","[class*='slider']","[class*='puzzle']"]:
            try:
                if drv.find_element(By.CSS_SELECTOR, sel).is_displayed():
                    found = True; break
            except: pass
        if not found:
            try: found = "슬라이더를 밀어" in drv.page_source
            except: pass
        if not found: return

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
            try: slider = drv.find_element(By.CSS_SELECTOR, s); break
            except: pass
        if slider:
            try:
                ac = ActionChains(drv)
                ac.click_and_hold(slider).pause(0.3)
                for _ in range(20): ac.move_by_offset(offset/20, 0).pause(0.02)
                ac.release().perform(); self._wait(1.2)
            except Exception as e: self.log(f"슬라이더 오류: {e}")

    # ── 구역 클릭 (iframe area 태그 + JS 겸용) ──
    def _click_zone(self, zone_num):
        drv = self.driver
        # 1) BookMain.asp (iframe) - area 태그로 클릭
        try:
            drv.switch_to.default_content()
            self._to_frame("ifrmSeat", "mainFrame")
            for area in drv.find_elements(By.TAG_NAME, "area"):
                t = (area.get_attribute("title") or
                     area.get_attribute("alt") or "").strip()
                if t == zone_num or t == zone_num + "구역" or t == zone_num + "영역":
                    drv.execute_script("arguments[0].click();", area)
                    drv.switch_to.default_content()
                    return True
            # href 방식
            for area in drv.find_elements(By.TAG_NAME, "area"):
                href = area.get_attribute("href") or ""
                if zone_num in href:
                    drv.execute_script(href.replace("javascript:", ""))
                    drv.switch_to.default_content()
                    return True
            drv.switch_to.default_content()
        except:
            try: drv.switch_to.default_content()
            except: pass

        # 2) motickets (SPA) - JS 텍스트/속성 검색
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
        self.log(f"구역 순회를 시작합니다 (딜레이 {self.delay}초)")
        while True:
            for zone in zones:
                self._wait(0)
                # 브라우저 세션 생존 확인
                try:
                    _ = self.driver.current_url
                except Exception:
                    self.log("브라우저가 종료되어 순회를 중단합니다."); return

                try:
                    # 구역번호 정규화: "가(001)"→"001", "206영역"→"206", "105구역"→"105"
                    zone_num = zone.replace("구역", "").replace("영역", "").strip()
                    if "(" in zone_num:
                        zone_num = zone_num.split("(")[-1].replace(")", "").strip()

                    # 구역 클릭 (실패해도 다음 구역으로)
                    if not self._click_zone(zone_num):
                        continue

                    self._wait(self.delay)
                    self._solve_puzzle()

                    # 예매 가능 좌석 클릭 → 성공하면 순회 종료
                    if self._click_seat(grade):
                        return
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
                self.log(f"구역 순회 {cycle}바퀴 완료...")

    # ── 티켓가격선택 클릭 ────────────────────
    def _click_complete(self):
        drv = self.driver
        self.log("티켓가격선택 버튼 찾는 중...")

        # 1) XPath로 다양한 태그에서 탐색
        keywords = ['티켓가격선택', '가격선택', '좌석선택완료', '선택완료']
        tags = ['button', 'a', 'div', 'span', 'p']
        clicked = False
        for kw in keywords:
            if clicked: break
            for tag in tags:
                xp = f"//{tag}[contains(text(),'{kw}')]"
                try:
                    els = drv.find_elements(By.XPATH, xp)
                    for el in els:
                        if el.is_displayed():
                            drv.execute_script("arguments[0].click();", el)
                            self.log(f"→ [{kw}] 클릭 완료")
                            clicked = True; break
                except: pass
                if clicked: break

        # 2) JS 텍스트 검색 (위에서 못 찾은 경우)
        if not clicked:
            js = r"""
            var kws = ['티켓가격선택','가격선택','좌석선택완료','선택완료'];
            var nodes = document.querySelectorAll('button,a,div,span,li');
            for(var k=0;k<kws.length;k++){
                for(var i=0;i<nodes.length;i++){
                    var t=(nodes[i].textContent||'').trim();
                    if(t===kws[k] || t.indexOf(kws[k])>=0){
                        var bnd=nodes[i].getBoundingClientRect();
                        if(bnd.width>0 && bnd.height>0){
                            nodes[i].click(); return kws[k];
                        }
                    }
                }
            }
            return null;
            """
            try:
                result = drv.execute_script(js)
                if result:
                    self.log(f"→ JS로 [{result}] 클릭 완료")
                    clicked = True
            except: pass

        if not clicked:
            self.log("→ 티켓가격선택 버튼을 찾지 못했습니다")

        self._wait(2.0)

        # 3) 클릭 후 페이지가 넘어갔는지 확인, 아직 좌석화면이면 재시도
        url = self._url()
        if "step2" in url and "티켓가격선택" in self._src():
            self.log("→ 아직 좌석 화면 - 1초 후 재시도")
            self._wait(1.0)
            try:
                js2 = r"""
                var nodes=document.querySelectorAll('button,a,div,span');
                for(var i=0;i<nodes.length;i++){
                    var t=(nodes[i].textContent||'').trim();
                    if(t.indexOf('티켓가격선택')>=0||t.indexOf('가격선택')>=0){
                        var b=nodes[i].getBoundingClientRect();
                        if(b.width>0&&b.height>0){nodes[i].click();return true;}
                    }
                }
                return false;
                """
                drv.execute_script(js2)
            except: pass
            self._wait(1.5)

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
            booking_kw = ("poticket", "Book", "motickets")
            try:
                known = len(self.driver.window_handles)
            except Exception:
                known = 1
            for _ in range(1800):
                self._wait(1)
                # 현재 창이 이미 예매 페이지면 완료
                try:
                    if any(k in self.driver.current_url for k in booking_kw):
                        break
                except Exception:
                    pass
                # 새 창이 열렸을 때만(개수 증가) 가장 최근 창으로 한 번 전환
                try:
                    handles = self.driver.window_handles
                    if len(handles) > known:
                        known = len(handles)
                        self.driver.switch_to.window(handles[-1])
                        self.log("→ 새 예매창으로 전환")
                        self._wait(1.5)
                except Exception:
                    pass
            self._wait(2)

            # ③ 안심예매 캡챠 처리
            if self._captcha_visible():
                self._handle_captcha()
                self._wait(1)

            # ④ 좌석 등급 선택 (페이지에서 동적으로 읽음)
            grade, grade_list = self._ask_grade()
            self._wait(0.3)

            # ⑤ 구역 선택 (선택한 등급 색상으로 필터링)
            zones = self._ask_zones(grade)
            self.log(f"선택 구역: {', '.join(zones) if zones else '전체'}")
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
