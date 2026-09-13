; 大肥鲸 VoiceAgent —— Windows 安装程序（Inno Setup 6）
;
; 别直接编译这个文件，走脚本：python scripts/build_installer.py
; （脚本负责准备 payload、下载简体中文语言文件、把版本号传进来。）
;
; 设计要点：
; 1) AppId 固定不变 —— 升级时是"替换安装"，不是装成两份；
; 2) 默认装到 {autopf}\VoiceAgent，没管理员权限时自动落到用户目录；
; 3) **不动用户数据**：卸载只删安装时装进去的文件，config.yaml / build/ /
;    apps.yaml / skills/ 这些运行期产生的东西留在原地（卸载时问一句要不要一起删）；
; 4) 超过 2GB 时必须分卷（DiskSpanning），所以产物可能是 setup.exe + setup-1.bin。

#ifndef AppVersion
  #define AppVersion "0.0"
#endif
#ifndef PayloadDir
  #define PayloadDir "..\dist\VoiceAgent"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\installer"
#endif
#ifndef LangFile
  #define LangFile ""
#endif
#ifndef IconFile
  #define IconFile ""
#endif

[Setup]
AppId={{8E31B7A4-5C2D-4F1B-9A77-6D2E0C4B12A9}
AppName=大肥鲸 VoiceAgent
AppVerName=大肥鲸 VoiceAgent {#AppVersion}
AppVersion={#AppVersion}
AppPublisher=voice-agent
AppPublisherURL=https://github.com/
AppSupportURL=https://github.com/
DefaultDirName={autopf}\VoiceAgent
DefaultGroupName=大肥鲸 VoiceAgent
DisableProgramGroupPage=yes
AllowNoIcons=yes
; 装到 Program Files 时程序写不了 build/（数据就放在 exe 旁边），所以不做"所有用户"
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=VoiceAgent-Setup-{#AppVersion}
VersionInfoVersion={#AppVersion}
VersionInfoProductName=大肥鲸 VoiceAgent
VersionInfoProductVersion={#AppVersion}
VersionInfoDescription=大肥鲸 VoiceAgent 安装程序
Compression=lzma2/fast
SolidCompression=no
DiskSpanning=yes
DiskSliceSize=max
WizardStyle=modern
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
UninstallDisplayName=大肥鲸 VoiceAgent {#AppVersion}
#if FileExists(IconFile)
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\voice-agent.ico
#endif
#if FileExists(LangFile)
DefaultLanguage=chinese
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
#if FileExists(LangFile)
Name: "chinese"; MessagesFile: "{#LangFile}"
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; config.yaml 单独一条：升级时**绝不覆盖**用户自己那份（里面有 API Key），
; 只在没有的时候放一份初始配置进去
Source: "{#PayloadDir}\config.yaml"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall; Check: FileExists(ExpandConstant('{#PayloadDir}\config.yaml'))
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Excludes: "config.yaml"; Flags: ignoreversion recursesubdirs createallsubdirs
#if FileExists(IconFile)
Source: "{#IconFile}"; DestDir: "{app}"; DestName: "voice-agent.ico"; Flags: ignoreversion
#endif

[Icons]
Name: "{group}\大肥鲸 VoiceAgent"; Filename: "{app}\VoiceAgent.exe"; WorkingDir: "{app}"
#if FileExists(IconFile)
Name: "{group}\大肥鲸 VoiceAgent"; Filename: "{app}\VoiceAgent.exe"; IconFilename: "{app}\voice-agent.ico"; WorkingDir: "{app}"
#endif
Name: "{group}\命令行版（doctor / selftest）"; Filename: "{app}\VoiceAgentCLI.exe"; Parameters: "doctor"; WorkingDir: "{app}"
Name: "{group}\卸载 大肥鲸 VoiceAgent"; Filename: "{uninstallexe}"
Name: "{autodesktop}\大肥鲸 VoiceAgent"; Filename: "{app}\VoiceAgent.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\VoiceAgent.exe"; Description: "现在启动 大肥鲸 VoiceAgent"; Flags: nowait postinstall skipifsilent
Filename: "{app}\VoiceAgentCLI.exe"; Parameters: "selftest"; Description: "跑一遍自检（不需要麦克风，约 10 秒）"; Flags: postinstall skipifsilent unchecked

[UninstallDelete]
; 刻意什么都不删：安装目录里属于"用户的"东西（config.yaml、build/、apps.yaml、
; skills/ 里的自定义技能）要留给用户。真要不要了，卸载时会问一句（见 [Code]）。

[Code]
var
  RemoveUserData: Boolean;

function InitializeUninstall(): Boolean;
var
  Answer: Integer;
begin
  Result := True;
  RemoveUserData := False;
  if DirExists(ExpandConstant('{app}\build')) or FileExists(ExpandConstant('{app}\config.yaml')) then
  begin
    Answer := MsgBox('要同时删掉你的配置和运行数据吗？' + #13#10 + #13#10 +
      '包括：config.yaml（里面有 API Key）、build\ 里的长期记忆 / 声纹 / 截图 / 日志。' + #13#10 +
      '选「否」就只卸载程序，这些留在原地，重装后还能接着用。',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2);
    RemoveUserData := (Answer = IDYES);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Root: String;
begin
  if (CurUninstallStep = usUninstall) and RemoveUserData then
  begin
    Root := ExpandConstant('{app}');
    DelTree(Root + '\build', True, True, True);
    DelTree(Root + '\screenshots', True, True, True);
    DelTree(Root + '\vision', True, True, True);
    DelTree(Root + '\skills', True, True, True);
    DelTree(Root + '\tools', True, True, True);
    DeleteFile(Root + '\config.yaml');
    DeleteFile(Root + '\config.yaml.bak');
    DeleteFile(Root + '\apps.yaml');
    DeleteFile(Root + '\audit.jsonl');
  end;
end;
