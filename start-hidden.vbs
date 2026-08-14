On Error Resume Next

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
q = Chr(34)

' Stop any existing instance first (avoids port 18900 conflict)
shell.Run "cmd /c " & q & dir & "\stop.bat" & q, 0, True

' Launch Mclaw server hidden; console output goes to logs\mclaw-console.log
' (NOT logs\mclaw.log — that file is owned by Mclaw's own RotatingFileHandler,
'  so redirecting into it would collide and crash the server on startup)
shell.Run "cmd /c cd /d " & q & dir & q & " && .venv\Scripts\mclaw.exe serve >> " & q & dir & "\logs\mclaw-console.log" & q & " 2>&1", 0, False
