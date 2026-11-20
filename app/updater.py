"""อัปเดตตัวเอง: อ่าน latest.json จาก GitHub → ดาวน์โหลดโปรแกรม/ฐานข้อมูลใหม่"""
import json, os, subprocess, sys, tempfile, urllib.request

from core import USER_DIR, VERSION

REPO = "chanagundpt/inci-check"
LATEST_URL = "https://raw.githubusercontent.com/%s/main/latest.json" % REPO
UA = {"User-Agent": "2NK-INCI-Check/" + VERSION}


def vtuple(v):
    try:
        return tuple(int(x) for x in str(v).lstrip("v").split("."))
    except Exception:
        return (0,)


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check(db_version):
    info = json.loads(fetch(LATEST_URL + "?t=%d" % int(__import__("time").time())))
    return {
        "latest": info,
        "app_new": vtuple(info.get("app", "0")) > vtuple(VERSION),
        "db_new": str(info.get("db", "")) > str(db_version or ""),
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def download(url, dest, progress=None):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            while True:
                b = r.read(256 * 1024)
                if not b:
                    break
                f.write(b)
                done += len(b)
                if progress and total:
                    progress(done * 100 // total)
    os.replace(tmp, dest)
    return dest


def update_db(info, progress=None):
    """ดาวน์โหลด regdb.json + ไฟล์ประกาศที่ยังไม่มี ไว้ใน %APPDATA%"""
    regs = os.path.join(USER_DIR, "regs")
    os.makedirs(regs, exist_ok=True)
    for name, url in (info.get("files") or {}).items():
        dest = os.path.join(regs, name)
        if not os.path.exists(dest):
            download(url, dest)
    download(info["db_url"], os.path.join(USER_DIR, "regdb.json"), progress)


def update_app(info, progress=None):
    """ดาวน์โหลด exe ใหม่ แล้วให้สคริปต์แทนที่ไฟล์หลังโปรแกรมปิด"""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("อัปเดตโปรแกรมได้เฉพาะไฟล์ .exe")
    exe = sys.executable
    new = os.path.join(USER_DIR, "update.exe")
    download(info["app_url"], new, progress)
    bat = os.path.join(tempfile.gettempdir(), "2nk_inci_update.cmd")
    with open(bat, "w", encoding="mbcs", errors="replace") as f:
        f.write('@echo off\r\n'
                ':wait\r\ntasklist /FI "PID eq %d" | find "%d" >nul && (timeout /t 1 >nul & goto wait)\r\n'
                'move /Y "%s" "%s" >nul\r\nstart "" "%s"\r\ndel "%%~f0"\r\n' % (os.getpid(), os.getpid(), new, exe, exe))
    subprocess.Popen(["cmd", "/c", bat], creationflags=0x08000000 | 0x00000008)  # no window, detached
    return True
