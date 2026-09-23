#define MyAppName "SquishIt"
#define MyAppVersion "2.0.1"
#define MyAppPublisher "Vlad"
#define MyAppExeName "SquishIt.exe"

[Setup]
AppId={{7C78E30E-54D8-488E-AFC9-AF2800B03EAB}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
LicenseFile=..\LICENSE
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=SquishIt-Setup-v{#MyAppVersion}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\squishit.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"
Name: "contextmenu"; Description: "Add Explorer right-click menu"; GroupDescription: "Shell integration:"; Flags: checkedonce
Name: "startuptray"; Description: "Start SquishIt tray with Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\dist\SquishIt\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--register-context"; Flags: runhidden; Tasks: contextmenu
Filename: "{app}\{#MyAppExeName}"; Parameters: "--register-startup"; Flags: runhidden; Tasks: startuptray
Filename: "{app}\{#MyAppExeName}"; Description: "Launch SquishIt"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--unregister-context"; Flags: runhidden; RunOnceId: "UnregContext"
Filename: "{app}\{#MyAppExeName}"; Parameters: "--unregister-startup"; Flags: runhidden; RunOnceId: "UnregStartup"
