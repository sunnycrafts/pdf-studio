; Inno Setup script for PDF Studio — builds a standalone installer with
; Start Menu + optional Desktop shortcuts. Compile with ISCC.exe installer.iss
#define MyAppName "PDF Studio"
#define MyAppVersion "1.0"
#define MyAppPublisher "PDF Studio"
#define MyAppExe "PDF Studio.exe"
; Path to the built one-file EXE to package. Overridable from the ISCC command
; line with /DSourceExe="<abs path>"; defaults to the old in-folder location.
#ifndef SourceExe
  #define SourceExe "..\PDF Studio.exe"
#endif

[Setup]
AppId={{8F3A9C42-5B7D-4E61-9A2C-7D11E2F4A8B0}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExe}
OutputDir=..\
OutputBaseFilename=PDF Studio Setup
SetupIconFile=icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
PrivilegesRequired=lowest

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
Source: "{#SourceExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
