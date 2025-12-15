Set WshShell = CreateObject("WScript.Shell") 
WshShell.Run chr(34) & "C:\AlivAgent\iniciar_agente.bat" & chr(34), 0
Set WshShell = Nothing