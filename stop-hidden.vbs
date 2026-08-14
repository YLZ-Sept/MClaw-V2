On Error Resume Next

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
q = Chr(34)

shell.Run q & dir & "\stop.bat" & q, 0, True

MsgBox "Mclaw service stopped (port 18900).", 64, "Mclaw"
