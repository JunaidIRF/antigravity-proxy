' Launch antigravity_proxy.py silently in background using pythonw
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
proxyScript = Chr(34) & scriptDir & "\antigravity_proxy.py" & Chr(34)

Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = scriptDir
WshShell.Run "pythonw " & proxyScript, 0, False

WScript.Echo "Antigravity Proxy started in the background on http://127.0.0.1:8877/v1." & vbCrLf & "Use status.bat to check health, or stop_proxy.bat to stop it."
