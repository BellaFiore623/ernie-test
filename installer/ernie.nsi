; The installer. Built by `python build.py --installer`, which passes the
; version in rather than letting this file hold a second copy of it.
;
;     makensis /DAppVersion=0.9.0 installer\ernie.nsi
;
; **Per-user, and that is the whole shape of it.** No admin, no UAC prompt,
; no Program Files -- which matters three times over: nobody here can elevate
; on request, an unsigned installer asking for admin is exactly what somebody
; is right to refuse, and the application writes its database beside itself in
; spirit anyway. So the program goes to %LOCALAPPDATA%\Programs\Ernie and the
; board lives on in %LOCALAPPDATA%\Ernie, which is where `ernie_sync.CONFIG_DIR`
; already points.
;
; **Those two directories are deliberately not the same one.** The program
; directory is disposable -- wiped and rewritten on every upgrade, removed
; entirely on uninstall -- and the data directory holds the database, the log
; and the env file with the token in it. Keeping them apart is what lets the
; upgrade be brutal and the uninstall be safe.
;
; **No secret is in here.** The env file is not installed, generated or
; prompted for: a token baked into an installer that lives in a shared Drive
; folder is a token shared with everyone who can reach that folder, and it
; would be the second copy of it -- which is how the last set got mixed up.
; The application says what is missing and where to put it on first run.

Unicode true

!ifndef AppVersion
  !define AppVersion "0.0.0"
!endif

!define AppName    "Ernie"
!define AppExe     "Ernie.exe"
!define Publisher  "Edge AI Solutions"
; The same mutex `ernie_app.take_lock()` holds. Named here so the installer
; can tell whether the thing it is about to overwrite is running -- Windows
; locks a running exe, so without this an upgrade fails halfway and leaves a
; half-written program directory.
!define AppMutex   "Local\ErnieBert"
!define RegKey     "Software\Microsoft\Windows\CurrentVersion\Uninstall\${AppName}"

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"

Name "${AppName} ${AppVersion}"
OutFile "..\dist\Ernie-${AppVersion}-setup.exe"
InstallDir "$LOCALAPPDATA\Programs\${AppName}"
; Per-user: asking for admin would put the files somewhere the application
; cannot write to and cost a UAC prompt on an unsigned binary, which is the
; prompt people are right to refuse.
RequestExecutionLevel user
SetCompressor /SOLID lzma
ShowInstDetails show

VIProductVersion "${AppVersion}.0"
VIAddVersionKey "ProductName"     "${AppName}"
VIAddVersionKey "FileDescription" "${AppName} + Bert"
VIAddVersionKey "FileVersion"     "${AppVersion}"
VIAddVersionKey "CompanyName"     "${Publisher}"
; NSIS warns without it, and an unsigned binary with a blank version
; tab is one more thing for SmartScreen and a curious person to dislike.
VIAddVersionKey "LegalCopyright"  "${Publisher}"

!define MUI_ICON   "..\assets\ernie.ico"
!define MUI_UNICON "..\assets\ernie.ico"
!define MUI_ABORTWARNING

!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
; Ticked by default: somebody who just installed it wants to see it open, and
; the first run is also what tells them if the env file is missing.
!define MUI_FINISHPAGE_RUN "$INSTDIR\${AppExe}"
!define MUI_FINISHPAGE_RUN_TEXT "Open ${AppName}"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"


;--------------------------------------------------------------------------
; Refuse to overwrite a copy that is running.
;
; Retried rather than aborted, because the answer is one click away in the
; other window and making somebody restart the installer to use it is rude.
; Checked in both the installer and the uninstaller: the same file is locked
; either way.
;--------------------------------------------------------------------------
!macro EnsureClosed un
Function ${un}EnsureClosed
  retry:
    System::Call 'kernel32::OpenMutex(i 0x00100000, b 0, t "${AppMutex}") i .r0'
    ${If} $0 <> 0
      System::Call 'kernel32::CloseHandle(i $0)'
      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION \
        "${AppName} is running, and Windows will not let it be replaced while \
it is.$\r$\n$\r$\nClose its window, then choose Retry.$\r$\n$\r$\nClosing it \
also sends anything it still owes to Discord, so give it a moment to finish." \
        IDRETRY retry
      Abort "Aborted: ${AppName} was still running."
    ${EndIf}
FunctionEnd
!macroend
!insertmacro EnsureClosed ""
!insertmacro EnsureClosed "un."


Section "Install"
  Call EnsureClosed

  ; **Wiped, not merged.** PyInstaller's _internal changes shape between
  ; builds, so copying over the top leaves orphaned DLLs from the previous
  ; version sitting beside the new ones -- and an orphaned DLL that still
  ; loads is the worst kind, because it works until it doesn't. The data
  ; directory is somewhere else entirely, so there is nothing here to lose.
  RMDir /r "$INSTDIR"
  CreateDirectory "$INSTDIR"

  SetOutPath "$INSTDIR"
  File /r "..\dist\Ernie\*.*"

  CreateShortCut "$SMPROGRAMS\${AppName}.lnk" "$INSTDIR\${AppExe}"

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; HKCU, matching the per-user install: this is the Add/Remove Programs
  ; entry, and writing it to HKLM would need the admin this deliberately
  ; does not ask for.
  WriteRegStr HKCU "${RegKey}" "DisplayName"     "${AppName}"
  WriteRegStr HKCU "${RegKey}" "DisplayVersion"  "${AppVersion}"
  WriteRegStr HKCU "${RegKey}" "Publisher"       "${Publisher}"
  WriteRegStr HKCU "${RegKey}" "DisplayIcon"     "$INSTDIR\${AppExe}"
  WriteRegStr HKCU "${RegKey}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${RegKey}" "UninstallString" "$\"$INSTDIR\Uninstall.exe$\""
  WriteRegDWORD HKCU "${RegKey}" "NoModify" 1
  WriteRegDWORD HKCU "${RegKey}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${RegKey}" "EstimatedSize" "$0"
SectionEnd


Section "Uninstall"
  Call un.EnsureClosed

  RMDir /r "$INSTDIR"
  Delete "$SMPROGRAMS\${AppName}.lnk"
  DeleteRegKey HKCU "${RegKey}"

  ; **The board is not ours to delete.** %LOCALAPPDATA%\Ernie holds the
  ; database, the log and the env file, and an uninstall is usually somebody
  ; reinstalling. Offered rather than assumed, and defaulting to keeping it:
  ; the cost of keeping data nobody wanted is a few megabytes, and the cost
  ; of deleting data somebody did want is their board.
  ; A silent uninstall has nobody to answer, and the unanswerable question
  ; must not be asked -- an /S run would hang on a dialog nobody can see.
  ; Keeping the data is the safe way to not ask.
  IfSilent done
  IfFileExists "$LOCALAPPDATA\${AppName}\*.*" 0 done
    MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 \
      "Also delete ${AppName}'s data?$\r$\n$\r$\nThat is the local database, \
the log, and the settings file with your Discord token in it, in$\r$\n\
$LOCALAPPDATA\${AppName}$\r$\n$\r$\nKeep it if you are reinstalling. Nothing \
on Discord is affected either way." \
      IDNO done
    RMDir /r "$LOCALAPPDATA\${AppName}"
  done:
SectionEnd
