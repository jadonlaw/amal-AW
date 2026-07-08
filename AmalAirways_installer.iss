; Amal Airways FMS — Windows Installer (Inno Setup)
; Inno Setup is FREE: https://jrsoftware.org/isdl.php
; After building AmalAirways.exe, open this file in Inno Setup and click Compile.
; It produces AmalAirways_Setup.exe — a real installer with a Start Menu shortcut.

[Setup]
AppName=Amal Airways FMS
AppVersion=1.0
DefaultDirName={autopf}\Amal Airways FMS
DefaultGroupName=Amal Airways FMS
OutputBaseFilename=AmalAirways_Setup
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes
; installs per-user, no admin needed:
PrivilegesRequired=lowest

[Files]
Source: "dist\AmalAirways.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Amal Airways FMS"; Filename: "{app}\AmalAirways.exe"
Name: "{userdesktop}\Amal Airways FMS"; Filename: "{app}\AmalAirways.exe"

[Run]
Filename: "{app}\AmalAirways.exe"; Description: "Launch Amal Airways FMS"; Flags: nowait postinstall skipifsilent
