; AmviBackUp installer script (Inno Setup 6.3 or newer). One installer for first install, update and repair.
; Build it with:  py installer\build_installer.py --app amvibackup_22_0.py
; The files in installer\generated are created by the build script. Do not edit them by hand.

#define AppName "AmviBackUp"
#define AppExe "AmviBackUp.exe"
#define AppAuthor "Maxim Dubkov (AmviSoft)"
#define AppCopyright "Copyright (C) 2026 Maxim Dubkov (AmviSoft)"
#define AppMutexName "AmviBackUpAppMutex"
; Must be the same as APP_USER_MODEL_ID in the application: shortcuts, taskbar and the running program share one identity.
#define AppUserModelId "AmviBackUp.App"
; The AppId GUID must never change: the installed version is found by it.
#define AppIdValue "{{B3E1D6A2-5C7F-4E0B-9A41-7D2F8C6E1A55}"

#ifndef AppVersionFull
  #define AppVersionFull "22.0-beta"
#endif
#ifndef AppVersionNumeric
  #define AppVersionNumeric "22.0.0.0"
#endif
#ifndef DistDir
  #define DistDir "generated\dist\AmviBackUp"
#endif

[Setup]
AppId={#AppIdValue}
AppName={#AppName}
AppVersion={#AppVersionFull}
VersionInfoVersion={#AppVersionNumeric}
AppPublisher={#AppAuthor}
AppCopyright={#AppCopyright}
VersionInfoCompany={#AppAuthor}
VersionInfoCopyright={#AppCopyright}
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}
DefaultDirName={localappdata}\Programs\{#AppName}
UsePreviousAppDir=yes
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableWelcomePage=no
DisableDirPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
AppMutex={#AppMutexName}
CloseApplications=no
RestartApplications=no
ShowLanguageDialog=auto
LanguageDetectionMethod=uilanguage
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
SetupIconFile=AmviBackUp.ico
UninstallDisplayIcon={app}\{#AppExe}
OutputDir=output
OutputBaseFilename={#AppName}-Setup-{#AppVersionFull}

#include "generated\languages.iss"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelId}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon; AppUserModelID: "{#AppUserModelId}"

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  DataFolderName = 'AmviBackUp';
  DatabaseFileName = 'amvibackup.sqlite3';
  LanguageMarkerName = 'installer-language.txt';
  SupportFirstRunMarkerName = 'support-first-run.txt';
  AutostartKeyName = 'Software\Microsoft\Windows\CurrentVersion\Run';
  AutostartValueName = 'AmviBackUp';
  UninstallKeyName = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{B3E1D6A2-5C7F-4E0B-9A41-7D2F8C6E1A55}_is1';

var
  InstalledVersion: String;

// Takes the part before the first dash, then compares up to four numbers.
function StripSuffix(const Version: String): String;
var
  Position: Integer;
begin
  Result := Trim(Version);
  Position := Pos('-', Result);
  if Position > 0 then
    Result := Copy(Result, 1, Position - 1);
end;

function TakeNumber(var Rest: String): Integer;
var
  Position: Integer;
  Token: String;
begin
  Position := Pos('.', Rest);
  if Position = 0 then
  begin
    Token := Rest;
    Rest := '';
  end
  else
  begin
    Token := Copy(Rest, 1, Position - 1);
    Delete(Rest, 1, Position);
  end;
  Result := StrToIntDef(Token, 0);
end;

function CompareVersions(const First, Second: String): Integer;
var
  FirstRest, SecondRest: String;
  Index, FirstNumber, SecondNumber: Integer;
begin
  Result := 0;
  FirstRest := StripSuffix(First);
  SecondRest := StripSuffix(Second);
  for Index := 1 to 4 do
  begin
    FirstNumber := TakeNumber(FirstRest);
    SecondNumber := TakeNumber(SecondRest);
    if FirstNumber < SecondNumber then
    begin
      Result := -1;
      Exit;
    end;
    if FirstNumber > SecondNumber then
    begin
      Result := 1;
      Exit;
    end;
  end;
end;

function ReadInstalledVersion: String;
var
  Value: String;
begin
  Result := '';
  if RegQueryStringValue(HKCU, UninstallKeyName, 'DisplayVersion', Value) then
    Result := Value
  else if RegQueryStringValue(HKLM, UninstallKeyName, 'DisplayVersion', Value) then
    Result := Value;
end;

function IsUpgrade: Boolean;
begin
  Result := (InstalledVersion <> '') and (CompareVersions(InstalledVersion, '{#AppVersionNumeric}') < 0);
end;

function IsDowngrade: Boolean;
begin
  Result := (InstalledVersion <> '') and (CompareVersions(InstalledVersion, '{#AppVersionNumeric}') > 0);
end;

function IsReinstall: Boolean;
begin
  Result := (InstalledVersion <> '') and (CompareVersions(InstalledVersion, '{#AppVersionNumeric}') = 0);
end;

// Fills %1 and %2 and turns any leftover percent-n marker into a line break.
function FormatText(const Text, First, Second: String): String;
begin
  Result := FmtMessage(Text, [First, Second]);
  StringChangeEx(Result, '%n', #13#10, True);
end;

function DataFolderPath: String;
begin
  Result := ExpandConstant('{localappdata}') + '\' + DataFolderName;
end;

procedure CopyIfExists(const Source, TargetDir, FileName: String);
begin
  if FileExists(Source) then
  begin
    if not FileCopy(Source, TargetDir + '\' + FileName, False) then
      Log('Database copy failed: ' + Source);
  end;
end;

// Best effort safety copy of the database before program files are replaced.
// Setup never changes the database itself, so a failed copy does not stop the update.
procedure BackupUserDatabase(const OldVersion, NewVersion: String);
var
  BackupDir, DatabasePath: String;
begin
  DatabasePath := DataFolderPath + '\' + DatabaseFileName;
  if not FileExists(DatabasePath) then
    Exit;
  BackupDir := DataFolderPath + '\recovery\pre-update-' + OldVersion + '-to-' + NewVersion;
  if not ForceDirectories(BackupDir) then
  begin
    Log('Cannot create backup folder: ' + BackupDir);
    Exit;
  end;
  CopyIfExists(DatabasePath, BackupDir, DatabaseFileName);
  CopyIfExists(DatabasePath + '-wal', BackupDir, DatabaseFileName + '-wal');
  CopyIfExists(DatabasePath + '-shm', BackupDir, DatabaseFileName + '-shm');
end;

// The application reads this file once on its first start and then removes it.
procedure WriteLanguageMarker;
begin
  if ForceDirectories(DataFolderPath) then
    SaveStringToFile(DataFolderPath + '\' + LanguageMarkerName, ActiveLanguage, False);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpWelcome) and IsDowngrade then
    Result := False;
end;

function InitializeSetup: Boolean;
begin
  InstalledVersion := ReadInstalledVersion;
  Log('Installed version: ' + InstalledVersion);
  Result := True;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpWelcome then
  begin
    if IsDowngrade then
    begin
      WizardForm.WelcomeLabel2.Caption := FormatText(CustomMessage('AmviDowngrade'), InstalledVersion, '{#AppVersionFull}');
      WizardForm.NextButton.Enabled := False;
    end
    else if IsUpgrade then
    begin
      WizardForm.WelcomeLabel2.Caption := FormatText(CustomMessage('AmviUpdateWelcome'), InstalledVersion, '{#AppVersionFull}');
      WizardForm.NextButton.Caption := CustomMessage('AmviUpdateButton');
    end
    else if IsReinstall then
    begin
      WizardForm.WelcomeLabel2.Caption := FormatText(CustomMessage('AmviReinstallWelcome'), InstalledVersion, '{#AppVersionFull}');
      WizardForm.NextButton.Caption := CustomMessage('AmviUpdateButton');
    end
    else
      WizardForm.NextButton.Caption := SetupMessage(msgButtonInstall);
  end;
  if (CurPageID = wpSelectTasks) and (IsUpgrade or IsReinstall) then
    WizardForm.NextButton.Caption := CustomMessage('AmviUpdateButton');
  if (CurPageID = wpFinished) and IsUpgrade then
    WizardForm.FinishedLabel.Caption := FormatText(CustomMessage('AmviUpdateFinished'), InstalledVersion, '{#AppVersionFull}');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if IsDowngrade then
    Result := FormatText(CustomMessage('AmviDowngrade'), InstalledVersion, '{#AppVersionFull}')
  else if CheckForMutexes('{#AppMutexName}') then
    Result := FormatText(CustomMessage('AmviCloseApp'), '', '');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    if InstalledVersion <> '' then
      BackupUserDatabase(InstalledVersion, '{#AppVersionFull}');
  end;
  if CurStep = ssPostInstall then
  begin
    if InstalledVersion = '' then
    begin
      WriteLanguageMarker;
      if ForceDirectories(DataFolderPath) then
        SaveStringToFile(DataFolderPath + '\' + SupportFirstRunMarkerName, '1', False);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RegDeleteValue(HKCU, AutostartKeyName, AutostartValueName);
    if not UninstallSilent then
    begin
      if MsgBox(FormatText(CustomMessage('AmviUninstallData'), '', ''), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataFolderPath, True, True, True);
    end;
  end;
end;
