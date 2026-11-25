; Inno Setup script — build with:
;   iscc packaging\installer.iss
; (Inno Setup 6: https://jrsoftware.org/isdl.php)
; Produces installer\akin-video-finder-setup.exe
; ffmpeg/ffprobe ship *inside* akin-video-finder-gui.exe when present in
; packaging\bin\ at PyInstaller build time — nothing extra to install.

#define AppName "Akin Video Finder"
#define AppVersion "0.3.0"
#define AppPublisher "Akin Video Finder Contributors"

[Setup]
AppId={{8F2E4C1A-3B5D-4E6F-9A1B-2C3D4E5F6A7B}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\installer
OutputBaseFilename=akin-video-finder-setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\LICENSE
UninstallDisplayIcon={app}\akin-video-finder-gui.exe
SetupIconFile=..\resources\icons\akin-video-finder.ico

[Files]
Source: "..\dist\akin-video-finder-gui.exe"; DestDir: "{app}"
Source: "..\LICENSE"; DestDir: "{app}"
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"
Source: "..\licenses\*"; DestDir: "{app}\licenses"; Flags: recursesubdirs createallsubdirs

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\akin-video-finder-gui.exe"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\akin-video-finder-gui.exe"; Tasks: desktopicon
