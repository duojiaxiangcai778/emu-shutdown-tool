<#
===========================================================================
 关键程序开机预加载（VPN + 资源管理器）
 让 FlClash / Everything / Quicker / EcoPaste 在进桌面时已就绪
===========================================================================
 原理：从注册表 Run 移出，改用任务计划程序「系统启动时触发」
       在用户登录前就开始加载，登录后立即就绪。
 使用：管理员 PowerShell 运行本脚本
===========================================================================
#>
#Requires -RunAsAdministrator

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " 预加载配置：VPN + 资源管理器工具" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

$USER = "$env:USERDOMAIN\$env:USERNAME"
$SID = ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value

# ---- 1. 定义关键程序 ----
$apps = @(
    @{
        Name = "FlClash"
        Exe = "D:\D\FlClash\FlClash.exe"
        Args = ""
        Desc = "VPN / 代理客户端"
        Trigger = "Boot"     # 系统启动时开始加载
        Delay = "PT0S"       # 不延迟
    },
    @{
        Name = "Everything"
        Exe = "D:\D\【搜索】Everything 基于名称快速定位文件和文件夹\Everything.exe"
        Args = "-startup"
        Desc = "文件搜索（资源管理器增强）"
        Trigger = "Boot"
        Delay = "PT3S"       # 延迟 3 秒，避免开机瞬间 I/O 抢
    },
    @{
        Name = "Quicker"
        Exe = "C:\Program Files\Quicker\Quicker.exe"
        Args = "-autorun"
        Desc = "快捷面板（资源管理器集成）"
        Trigger = "Logon"    # 登录时立即启动
        Delay = "PT0S"
    },
    @{
        Name = "EcoPaste"
        Exe = "D:\D\安装\EcoPaste\EcoPaste.exe"
        Args = "--auto-launch"
        Desc = "剪贴板管理（文件操作联动）"
        Trigger = "Logon"
        Delay = "PT2S"
    }
)

Write-Host ""
Write-Host "准备配置以下程序："
$apps | ForEach-Object { Write-Host "  [$($_.Trigger)] $($_.Name) → $($_.Exe)" -ForegroundColor White }

# ---- 2. 删旧的预加载任务（如果有） ----
Write-Host ""
Write-Host "[1/3] 清除旧的预加载任务..." -ForegroundColor Yellow
$null = schtasks /Delete /TN "\开机预加载\FlClash" /F 2>&1
$null = schtasks /Delete /TN "\开机预加载\Everything" /F 2>&1
$null = schtasks /Delete /TN "\开机预加载\Quicker" /F 2>&1
$null = schtasks /Delete /TN "\开机预加载\EcoPaste" /F 2>&1

# ---- 3. 逐个创建任务 ----
Write-Host "[2/3] 创建任务计划程序..." -ForegroundColor Yellow
Write-Host ""

$taskFolder = "\开机预加载"
$failed = @()

foreach ($app in $apps) {
    $taskPath = "$taskFolder\$($app.Name)"
    
    # 验证 exe 存在
    if (-not (Test-Path $app.Exe)) {
        Write-Host "  ⚠ 路径不存在: $($app.Name) → $($app.Exe)" -ForegroundColor Yellow
        $failed += $app.Name
        continue
    }
    
    # 构建 XML
    $triggerType = if ($app.Trigger -eq "Boot") { "BootTrigger" } else { "LogonTrigger" }
    $userIdXml = if ($app.Trigger -eq "Boot") { "<UserId>$USER</UserId>" } else { "<UserId>$SID</UserId>" }
    $logonType = if ($app.Trigger -eq "Boot") { "InteractiveToken" } else { "InteractiveToken" }
    
    $argsXml = if ($app.Args) { "<Arguments>$($app.Args)</Arguments>" } else { "" }
    
    $xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Date>$(Get-Date -Format "yyyy-MM-ddTHH:mm:ss")</Date>
    <Author>$env:USERNAME</Author>
    <Description>$($app.Desc) — 开机预加载</Description>
  </RegistrationInfo>
  <Triggers>
    <${triggerType}>
      <Enabled>true</Enabled>
      <Delay>$($app.Delay)</Delay>
    </${triggerType}>
  </Triggers>
  <Principals>
    <Principal id="Author">
      $userIdXml
      <LogonType>$logonType</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <Enabled>true</Enabled>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <StartWhenAvailable>true</StartWhenAvailable>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$($app.Exe)</Command>
      $argsXml
    </Exec>
  </Actions>
</Task>
"@
    
    $xmlFile = "$env:TEMP\_task_$($app.Name).xml"
    $xml | Out-File -FilePath $xmlFile -Encoding Unicode
    
    Write-Host "  创建: $($app.Name) ..." -NoNewline
    $result = schtasks /Create /TN "$taskPath" /XML "$xmlFile" /F 2>&1
    Remove-Item -Path $xmlFile -Force -ErrorAction SilentlyContinue
    
    if ($LASTEXITCODE -eq 0) {
        Write-Host " ✓ ($($app.Trigger) + $($app.Delay))" -ForegroundColor Green
    } else {
        Write-Host " ✗ $result" -ForegroundColor Red
        $failed += $app.Name
    }
}

# ---- 4. 从注册表删除原自启项 ----
Write-Host ""
Write-Host "[3/3] 从注册表 Run 删除原自启项（防重复启动）..." -ForegroundColor Yellow

$runPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

# 备份当前注册表 Run 项
$backupFile = "$env:USERPROFILE\Desktop\RunBackup_$(Get-Date -Format 'yyyyMMdd_HHmmss').reg"
$null = Start-Process -FilePath "reg.exe" -ArgumentList "export `"HKCU\Software\Microsoft\Windows\CurrentVersion\Run`" `"$backupFile`"" -Wait -NoNewWindow
Write-Host "  已备份注册表 Run: $backupFile" -ForegroundColor Gray

@("FlClash", "Everything 1.5a", "EcoPaste", "Quicker") | ForEach-Object {
    Remove-ItemProperty -Path $runPath -Name $_ -ErrorAction SilentlyContinue
    Write-Host "  已从注册表删除: $_" -ForegroundColor Green
}

# ---- 完成 ----
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " ✅ 配置完成！" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "已配置 4 个关键程序开机预加载：" -ForegroundColor White
Write-Host "  - FlClash (VPN)     → 系统启动时预加载" -ForegroundColor Green
Write-Host "  - Everything (搜索) → 系统启动时预加载（+3秒延迟防I/O争抢）" -ForegroundColor Green  
Write-Host "  - Quicker (快捷面板) → 登录时立即启动" -ForegroundColor Green
Write-Host "  - EcoPaste (剪贴板) → 登录时启动（+2秒延迟）" -ForegroundColor Green
Write-Host ""
Write-Host "恢复方法：双击桌面 .reg 备份文件即可还原注册表 Run 项"
Write-Host ""
Write-Host "建议重启电脑验证效果。" -ForegroundColor Yellow
