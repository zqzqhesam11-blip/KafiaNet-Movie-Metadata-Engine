Option Explicit

Dim shell, fso, appFolder, psScript, command, exitCode
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appFolder = fso.GetParentFolderName(WScript.ScriptFullName)
psScript = fso.BuildPath(appFolder, "Start-KAFIANET-CONTROL-PRO.ps1")

If Not fso.FileExists(psScript) Then
    shell.Popup "The KAFIA NET CONTROL PRO PowerShell launcher is missing.", 0, "KAFIA NET CONTROL PRO", 16
    WScript.Quit 2
End If

command = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & psScript & Chr(34)

On Error Resume Next
exitCode = shell.Run(command, 0, True)
If Err.Number <> 0 Then
    shell.Popup "Unable to start the hidden application launcher. " & Err.Description, 0, "KAFIA NET CONTROL PRO", 16
    WScript.Quit 3
End If
On Error GoTo 0

WScript.Quit exitCode
