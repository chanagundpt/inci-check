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
    start_replacer(new, exe)
    return True


def start_replacer(new, exe, pids=None):
    """สคริปต์ PowerShell: รอโปรแกรมปิด (ทั้งตัวหลักและตัวเปิดของ PyInstaller) → แทนที่ไฟล์ → เปิดใหม่
    บันทึกผลใน %APPDATA%\\2NK INCI Check\\update.log"""
    pids = pids or [os.getpid(), os.getppid()]
    q = lambda s: "'" + s.replace("'", "''") + "'"
    log = os.path.join(USER_DIR, "update.log")
    ps = os.path.join(tempfile.gettempdir(), "2nk_inci_update.ps1")
    script = """$ErrorActionPreference = 'Continue'
$log = %s; $new = %s; $exe = %s
"$(Get-Date) start" | Out-File $log -Encoding utf8
foreach ($p in @(%s)) { try { Wait-Process -Id $p -Timeout 120 -ErrorAction Stop } catch {} }
$ok = $false
for ($i = 0; $i -lt 60 -and -not $ok; $i++) {
  try { Move-Item -LiteralPath $new -Destination $exe -Force -ErrorAction Stop; $ok = $true }
  catch { "$(Get-Date) retry $i : $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8; Start-Sleep -Milliseconds 500 }
}
"$(Get-Date) moved=$ok" | Out-File $log -Append -Encoding utf8
Start-Process -FilePath $exe
Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force
""" % (q(log), q(new), q(exe), ",".join(str(p) for p in pids))
    with open(ps, "w", encoding="utf-8-sig") as f:
        f.write(script)
    args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", ps]
    cmd = subprocess.list2cmdline(args)
    # โปรเซสลูกของ exe (PyInstaller) ถูกปิดตามเมื่อโปรแกรมปิด → ให้ WMI สร้างโปรเซสแทน (อยู่นอกโปรแกรม)
    # รอจนสร้างเสร็จก่อนปิดโปรแกรม (ทดสอบแล้ว 20 พ.ย.: Popen ธรรมดาสคริปต์ไม่ทำงาน, WMI ทำงาน)
    r = subprocess.run(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
                        "(Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=%s}).ReturnValue" % q(cmd)],
                       capture_output=True, text=True, timeout=60, creationflags=0x08000000)
    if r.stdout.strip() != "0":
        raise RuntimeError("เริ่มขั้นแทนที่ไฟล์ไม่ได้ (%s) — ไฟล์ใหม่อยู่ที่ %s" % ((r.stdout + r.stderr).strip()[:200], new))
