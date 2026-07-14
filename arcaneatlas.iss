; installer\arcaneatlas.iss  (this file is copied here by the build script)
#define MyAppName    "Arcane Atlas"
#define MyAppDirName "ArcaneAtlas"
; Version is the single source of truth in arcaneatlas\__init__.py — build_installer.bat
; extracts it and passes it as /DMyAppVersion. This fallback only applies to a bare
; ISCC run and should be kept roughly in sync.
#ifndef MyAppVersion
  #define MyAppVersion "0.8.0"
#endif
#define MyPublisher  "Eric Hernandez"
#define MyAppExeName "ArcaneAtlas.exe"

#define IconFile AddBackslash(SourcePath) + "..\\arcaneatlas\\resources\\installer.ico"
#define LicenseFile AddBackslash(SourcePath) + "..\\arcaneatlas\\resources\\license.txt"

[Setup]
; Choose ONE of these pairs:
; (A) Machine-wide install (recommended)
PrivilegesRequired=admin
DefaultDirName={autopf}\{#MyAppDirName}

; (B) Per-user install (uncomment both lines, and comment the two lines above)
;PrivilegesRequired=lowest
;DefaultDirName={userpf}\{#MyAppDirName}

CloseApplications=yes
RestartApplications=yes
DefaultGroupName={#MyAppName}
AppId={{A1F85F01-2B19-4DA5-AB4C-2CF0B3C0E5D2}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyPublisher}
WizardStyle=modern
Compression=lzma
SolidCompression=yes
UsePreviousAppDir=yes

; The output folder lives beside this .iss (i.e., installer\output)
OutputDir={#SourcePath}output
OutputBaseFilename=ArcaneAtlas-Setup-{#MyAppVersion}

; ---- Assets staged to installer\assets by the build script ----
#ifexist IconFile
  SetupIconFile={#IconFile}
#else
  #error "Installer Icon not found at: " + IconFile 
#endif

#ifexist LicenseFile
  LicenseFile={#LicenseFile}
#else
  #error "License File not found at: " + LicenseFile
#endif

UninstallDisplayIcon={app}\{#MyAppExeName}

[InstallDelete]
Type: filesandordirs; Name: "{app}\old-folder-or-file"

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
; PyInstaller output is one level UP from installer\  (dist\ArcaneAtlas\*)
Source: "{#SourcePath}..\dist\ArcaneAtlas\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent
