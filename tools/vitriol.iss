; Inno Setup script for Vitriol.
;
; Compile via:
;     python tools/build_installer.py
; which invokes iscc.exe with /DAppVersion=<version> from app/__version__.py.
;
; Pre-requisites on the build host:
;   - Inno Setup 6+ installed (https://jrsoftware.org/isdl.php).
;   - dist/Vitriol/ already built via tools/build_vitriol_dist.py
;     (the PyInstaller folder containing Vitriol.exe + _internal/).
;
; Output: dist/VitriolSetup-{AppVersion}.exe
;
; The setup.exe is unsigned by default — Windows SmartScreen will warn
; the user on first run. Code-signing with an EV cert is a separate
; manual post-step (not part of this build).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName       "Vitriol"
#define AppPublisher  "Equivalent Exchange"
#define AppExeName    "Vitriol.exe"
; Stable AppId (GUID). Inno uses this to identify the install across
; versions for upgrade / uninstall. Generate ONCE per app, never change.
#define AppId         "{{A50D8473-9AD2-43E5-92C8-8469E547244F}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
; Windows PE-level version metadata for the setup .exe itself.
; Without these, the installer file shows FileVersion 0.0.0.0 in the
; Properties pane even though AppVersion is set correctly. Inno needs
; a 4-part version (X.X.X.X) here, so we suffix .0 to AppVersion.
VersionInfoVersion={#AppVersion}.0
VersionInfoProductVersion={#AppVersion}.0
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
VersionInfoCompany={#AppPublisher}
VersionInfoCopyright=Copyright (C) 2026 {#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64
PrivilegesRequired=admin
; CloseApplications=yes — when the in-app updater hands a downloaded
; installer to Inno Setup, the running Vitriol.exe holds a file lock on
; its own image. This directive tells Inno to close any running
; instance before replacing files, so updates from inside the app
; complete without "another process has this file open" errors.
;
; RestartApplications=yes — re-launches Vitriol after install completes,
; so an in-app "Update now" feels seamless: app closes, installer runs,
; new app opens.
;
; The updater itself does subprocess.Popen(detached) + sys.exit(0)
; before launching the installer, so the close almost always happens
; without Inno needing to prompt.
CloseApplications=yes
RestartApplications=yes
OutputDir=..\dist
OutputBaseFilename=VitriolSetup-{#AppVersion}
SetupIconFile=..\resources\icons\logo.ico
; Show the Elastic License v2 as a wizard page that the user must accept
; before installation proceeds. Path is relative to this .iss file (in tools/).
LicenseFile=..\LICENSE
WizardStyle=modern
ShowLanguageDialog=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
; Ship the entire dist/Vitriol/ tree. Source paths are relative to
; this .iss file (which lives in tools/). recursesubdirs picks up
; _internal/ and everything under it.
Source: "..\dist\Vitriol\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; PyInstaller writes __pycache__ folders at runtime; remove them on
; uninstall so the app dir cleans up fully.
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\_internal\__pycache__"
