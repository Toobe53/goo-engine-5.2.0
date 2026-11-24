; Goo Engine 5.2.1 per-user Windows installer.
;
; Build with:
;   ISCC.exe tools\installer\windows\goo-engine.iss
; The source tree is the clean `_install-windows2` produced by `cmake --install`;
; no build or signing is performed here.
;
; This installer is intentionally unsigned until a real code-signing certificate
; is available. Do not describe the output as signed.

#define MyAppName "Goo Engine"
#ifndef MyAppVersion
  #define MyAppVersion "5.2.1"
#endif
#define MyAppPublisher "Goo Engine contributors"
#define MyAppURL "https://github.com/NaMgAl-Studio/goo-engine-5.2.0"
#define MyAppExeName "blender.exe"
#define MyAppId "{{6E2F5E3B-7A4C-4D9B-9A1F-2B6C8D0E4F71}"

#ifndef SourceDir
  #define SourceDir "..\..\..\_install-windows2"
#endif

#ifndef OutputDir
  #define OutputDir "..\..\..\_work\release-staging"
#endif

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\Goo Engine 5.2
DefaultGroupName=Goo Engine
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=goo-engine-{#MyAppVersion}-windows-x64-setup
SetupIconFile=..\..\..\release\windows\icons\winblender.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
VersionInfoVersion=5.2.1.0
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} {#MyAppVersion} installer (unsigned)

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName} {#MyAppVersion}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName} {#MyAppVersion}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
