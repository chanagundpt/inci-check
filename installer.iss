; Inno Setup — ตัวติดตั้ง 2NK INCI Check
; สร้าง: "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" /DAppVersion=x.y.z installer.iss
#define AppName "2NK INCI Check"
#ifndef AppVersion
  #define AppVersion "1.2.0"
#endif
#define AppExe "2NK INCI Check.exe"
#ifndef BuildDir
  #define BuildDir GetEnv("TEMP") + "\2nkInciBuild"
#endif

[Setup]
AppId={{B4E7C1D2-6A3F-4E8B-9D51-A1B2C3D4E5F6}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=2NK
; ติดตั้งในโฟลเดอร์ของผู้ใช้ (ไม่ต้องใช้สิทธิ์ผู้ดูแล) — โปรแกรมอัปเดตตัวเองได้
DefaultDirName={userpf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=installer
OutputBaseFilename=2NK-INCI-Check-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/fast
SolidCompression=no
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#BuildDir}\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName} now"; Flags: nowait postinstall skipifsilent
