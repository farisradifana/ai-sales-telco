' TELCO Sales AI - start the live WhatsApp stack at Windows logon, without any console window.
' PostgreSQL, 9Router, API, edge, n8n and the named Cloudflare tunnel "telco-wa" (see scripts/windows/telco_startup.sh).
' Install: copy this file into the Startup folder (Win+R -> shell:startup).
' Log: F:\ai-sales-telco\.local\startup.log
Option Explicit
Dim shell, fso, bash, script
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
bash = "C:\Program Files\Git\bin\bash.exe"
script = "/f/ai-sales-telco/scripts/windows/telco_startup.sh"
If Not fso.FileExists(bash) Then
  MsgBox "Git Bash tidak ditemukan: " & bash, vbExclamation, "TELCO Sales AI"
  WScript.Quit 1
End If
' 0 = hidden window, False = do not wait (the script keeps running as a watchdog)
shell.Run """" & bash & """ -lc """ & script & """", 0, False
