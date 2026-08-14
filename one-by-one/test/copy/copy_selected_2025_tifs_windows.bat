@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul

set "COPY_SELF=%~f0"
set "COPY_DIR=%~dp0"
set "COPY_NAMES=%~dp0name.txt"
set "COPY_PATHS=%~dp0path.txt"
set "COPY_JOBS=4"
if defined COPY_REQUESTED_JOBS set "COPY_JOBS=%COPY_REQUESTED_JOBS%"

if not exist "%COPY_NAMES%" (
    echo 错误：找不到名称清单：
    echo %COPY_NAMES%
    goto failed_before_start
)
if not exist "%COPY_PATHS%" (
    echo 错误：找不到路径配置：
    echo %COPY_PATHS%
    goto failed_before_start
)

set "COPY_TEMP_PS=%TEMP%\copy_selected_2025_%RANDOM%_%RANDOM%.ps1"

rem 从本 BAT 尾部提取内嵌的 PowerShell 程序。
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$raw=[IO.File]::ReadAllText($env:COPY_SELF,[Text.Encoding]::UTF8); $marker='# POWERSHELL_SCRIPT_START'; $pos=$raw.LastIndexOf($marker); if($pos -lt 0){exit 3}; [IO.File]::WriteAllText($env:COPY_TEMP_PS,$raw.Substring($pos+$marker.Length),[Text.Encoding]::UTF8)"
if errorlevel 1 (
    echo 错误：无法提取内嵌的 PowerShell 程序。
    goto failed_before_start
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%COPY_TEMP_PS%" -NamesPath "%COPY_NAMES%" -PathConfig "%COPY_PATHS%" -Jobs "%COPY_JOBS%"
set "COPY_EXIT_CODE=%ERRORLEVEL%"
del /q "%COPY_TEMP_PS%" >nul 2>&1
echo.
if not "%COPY_EXIT_CODE%"=="0" echo 运行失败，退出代码：%COPY_EXIT_CODE%
if /I "%COPY_NO_PAUSE%"=="1" exit /b %COPY_EXIT_CODE%
echo 按任意键关闭窗口...
pause >nul
exit /b %COPY_EXIT_CODE%

:failed_before_start
echo.
if /I "%COPY_NO_PAUSE%"=="1" exit /b 1
echo 按任意键关闭窗口...
pause >nul
exit /b 1

# POWERSHELL_SCRIPT_START
param(
    [Parameter(Mandatory = $true)][string]$NamesPath,
    [Parameter(Mandatory = $true)][string]$PathConfig,
    [ValidateRange(1, 32)][int]$Jobs = 4
)

$ErrorActionPreference = 'Stop'

$unusedEmbeddedPlaneNames = @'
K49E001022
K49E001023
K49E001024
K49E002016
K49E002017
K49E002019
K49E002022
K49E002023
K49E002024
K49E003017
K49E003019
K49E004017
K49E004019
K49E005018
K49E005019
K49E005020
K49E007018
K49E007019
K49E007020
K49E007021
K49E007024
K49E008016
K49E008018
K49E008019
K49E008020
K49E008024
K49E009017
K49E009018
K49E009019
K49E010017
K49E010020
K49E010021
K49E010022
K49E011019
K49E011020
K49E011021
K49E011022
K49E011023
K49E011024
K49E012019
K49E012020
K49E012021
K49E012022
K49E012023
K49E012024
K49E013020
K49E013021
K49E013024
K50E001001
K50E001004
K50E001005
K50E001008
K50E001009
K50E001010
K50E001011
K50E001012
K50E001013
K50E002001
K50E002006
K50E002007
K50E002008
K50E002010
K50E002011
K50E002012
K50E002013
K50E003004
K50E003005
K50E003007
K50E003008
K50E003009
K50E003010
K50E003011
K50E003012
K50E004007
K50E004009
K50E004010
K50E004011
K50E004012
K50E005004
K50E005007
K50E005009
K50E005010
K50E006005
K50E006006
K50E006007
K50E006008
K50E006009
K50E006010
K50E006011
K50E007003
K50E007004
K50E007005
K50E007006
K50E007007
K50E007008
K50E007009
K50E007010
K50E007011
K50E008002
K50E008003
K50E008004
K50E008005
K50E008006
K50E008007
K50E008008
K50E008009
K50E008010
K50E008011
K50E009001
K50E009002
K50E009003
K50E009004
K50E009005
K50E009006
K50E009007
K50E009008
K50E009009
K50E009010
K50E009011
K50E009012
K50E010002
K50E010003
K50E010004
K50E010005
K50E010006
K50E010007
K50E010008
K50E010009
K50E010010
K50E010011
K50E010012
K50E011001
K50E011002
K50E011003
K50E011004
K50E011005
K50E011006
K50E011007
K50E011008
K50E011009
K50E011010
K50E011011
K50E011012
K50E012001
K50E012002
K50E012004
K50E012005
K50E012006
K50E012007
K50E012008
K50E012009
K50E012010
K50E012011
K50E012012
K50E013001
K50E013004
K50E013005
K50E013006
K50E013007
K50E013008
K50E013009
K50E013010
K50E013011
K50E013012
K50E014004
K50E014005
K50E014006
K50E014007
K50E014008
K50E014009
K50E015004
K50E015005
K50E015006
L49E019019
L49E020021
L49E021018
L49E022019
L49E024021
L49E024022
L49E024023
L49E024024
L50E008024
L50E009023
L50E009024
L50E010021
L50E010022
L50E010023
L50E010024
L50E011020
L50E011021
L50E011022
L50E011023
L50E011024
L50E012020
L50E012021
L50E012022
L50E012023
L50E012024
L50E013015
L50E013020
L50E013021
L50E013022
L50E013023
L50E013024
L50E014013
L50E014020
L50E014021
L50E014022
L50E014023
L50E014024
L50E015010
L50E015012
L50E015013
L50E015022
L50E015023
L50E016011
L50E016012
L50E016022
L50E017008
L50E017011
L50E017014
L50E017019
L50E017022
L50E018005
L50E018009
L50E018017
L50E018018
L50E018020
L50E018021
L50E018022
L50E019005
L50E019009
L50E019010
L50E019011
L50E019014
L50E019018
L50E019019
L50E019020
L50E020005
L50E020008
L50E020011
L50E020014
L50E020015
L50E020016
L50E020018
L50E020019
L50E020020
L50E020021
L50E021001
L50E021006
L50E021009
L50E021010
L50E021011
L50E021013
L50E021014
L50E021015
L50E021016
L50E021017
L50E021019
L50E021020
L50E022007
L50E022008
L50E022009
L50E022010
L50E022011
L50E022013
L50E022014
L50E022015
L50E022016
L50E022017
L50E022019
L50E022020
L50E023004
L50E023008
L50E023009
L50E023010
L50E023011
L50E023012
L50E023013
L50E023014
L50E023015
L50E023016
L50E023017
L50E023018
L50E024004
L50E024008
L50E024009
L50E024010
L50E024011
L50E024012
L50E024013
L50E024015
L50E024016
L50E024017
L51E008001
L51E009001
'@ -split "`r?`n" | Where-Object { $_ }

$pathLines = @(
    Get-Content -LiteralPath $PathConfig -Encoding UTF8 |
        ForEach-Object { $_.Trim().Trim('"') } |
        Where-Object { $_ -and -not $_.StartsWith('#') }
)
if ($pathLines.Count -lt 2) {
    throw 'path.txt 至少需要两行：第1行输入根目录，第2行输出目录。'
}
$Root = $pathLines[0]
$Output = $pathLines[1]

$rawNames = @(
    Get-Content -LiteralPath $NamesPath -Encoding UTF8 |
        ForEach-Object { $_.Trim().Trim('"') } |
        Where-Object { $_ -and -not $_.StartsWith('#') }
)
if ($rawNames.Count -eq 0) {
    throw 'name.txt 中没有可用的图幅号。'
}

$nameSet = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
$planeNames = New-Object System.Collections.ArrayList
foreach ($rawName in $rawNames) {
    $planeName = $rawName
    if ($planeName.EndsWith('_2025.tif', [StringComparison]::OrdinalIgnoreCase)) {
        $planeName = $planeName.Substring(0, $planeName.Length - '_2025.tif'.Length)
    }
    if ($nameSet.Add($planeName)) {
        [void]$planeNames.Add($planeName)
    }
}

if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
    throw "根目录不存在：$Root"
}
$Root = (Resolve-Path -LiteralPath $Root).Path
[IO.Directory]::CreateDirectory($Output) | Out-Null
$Output = (Resolve-Path -LiteralPath $Output).Path

$wanted = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
foreach ($name in $planeNames) { [void]$wanted.Add("${name}_2025.tif") }

$found = New-Object 'System.Collections.Generic.Dictionary[string,System.IO.FileInfo]' ([StringComparer]::OrdinalIgnoreCase)
Write-Host "正在递归扫描：$Root"
Write-Host "目标文件数：$($planeNames.Count)"

Get-ChildItem -LiteralPath $Root -Filter '*_2025.tif' -File -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object {
    if ($wanted.Contains($_.Name) -and -not $found.ContainsKey($_.Name)) {
        $found.Add($_.Name, $_)
    }
}

$pending = New-Object System.Collections.ArrayList
$missing = New-Object System.Collections.Generic.List[string]
$existing = 0
[long]$totalBytes = 0

foreach ($planeName in $planeNames) {
    $fileName = "${planeName}_2025.tif"
    if (-not $found.ContainsKey($fileName)) {
        $missing.Add($fileName)
        continue
    }

    $file = $found[$fileName]
    $namePrefix = "$($file.BaseName)."
    $relatedFiles = @(
        Get-ChildItem -LiteralPath $file.DirectoryName -File -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name.StartsWith($namePrefix, [StringComparison]::OrdinalIgnoreCase) } |
            Sort-Object @{ Expression = { -not $_.Name.Equals($file.Name, [StringComparison]::OrdinalIgnoreCase) } }, Name
    )

    foreach ($relatedFile in $relatedFiles) {
        $destination = Join-Path $Output $relatedFile.Name
        if (Test-Path -LiteralPath $destination) {
            $existing++
            continue
        }

        [void]$pending.Add([pscustomobject]@{
            Source      = $relatedFile.FullName
            Destination = $destination
            Name        = $relatedFile.Name
            Length      = [long]$relatedFile.Length
        })
        $totalBytes += [long]$relatedFile.Length
    }
}

if ($missing.Count -gt 0) {
    $missingPath = Join-Path $Output 'missing_2025_tifs.txt'
    $missing | Sort-Object | Set-Content -LiteralPath $missingPath -Encoding UTF8
    Write-Host "未找到的文件名已写入：$missingPath"
}

Write-Host "扫描完成：源目录找到 $($found.Count) 个，未找到 $($missing.Count) 个。"
Write-Host "输出目录已有并跳过：$existing 个（含配套文件）。"
Write-Host ('本次待复制：{0} 个，共 {1:N2} GiB（{2:N2} GB）。' -f $pending.Count, ($totalBytes / 1GB), ($totalBytes / 1e9))
Write-Host "输出目录：$Output"
Write-Host "并发数：$Jobs"

if ($pending.Count -eq 0) {
    if ($found.Count -eq 0) { exit 1 }
    Write-Host '找到的目标文件均已存在于输出目录，无需复制。'
    exit 0
}

$copyWorker = {
    param($Item)

    $source = $Item.Source
    $destination = $Item.Destination
    $partial = "$destination.copying"
    $bufferSize = 8MB
    $buffer = New-Object byte[] $bufferSize
    $inputStream = $null
    $outputStream = $null

    try {
        if (Test-Path -LiteralPath $destination) {
            [pscustomobject]@{ Kind = 'Result'; Success = $true; Skipped = $true; Message = '目标已存在，跳过' }
            return
        }

        $inputStream = New-Object IO.FileStream($source, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read, $bufferSize, [IO.FileOptions]::SequentialScan)
        $outputStream = New-Object IO.FileStream($partial, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None, $bufferSize, [IO.FileOptions]::SequentialScan)

        [long]$copied = 0
        [long]$lastBytes = 0
        $timer = [Diagnostics.Stopwatch]::StartNew()
        $interval = [Diagnostics.Stopwatch]::StartNew()

        while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $outputStream.Write($buffer, 0, $read)
            $copied += $read

            if ($interval.ElapsedMilliseconds -ge 500) {
                $seconds = [Math]::Max($interval.Elapsed.TotalSeconds, 0.001)
                $speed = (($copied - $lastBytes) / 1MB) / $seconds
                $percent = if ($Item.Length -gt 0) { [Math]::Min(100, ($copied * 100.0 / $Item.Length)) } else { 100 }
                $eta = if ($speed -gt 0) { [TimeSpan]::FromSeconds((($Item.Length - $copied) / 1MB) / $speed) } else { [TimeSpan]::Zero }
                [pscustomobject]@{ Kind = 'Progress'; Bytes = $copied; Percent = $percent; Speed = $speed; Eta = $eta.ToString('hh\:mm\:ss') }
                $lastBytes = $copied
                $interval.Restart()
            }
        }

        $outputStream.Flush()
        $outputStream.Dispose()
        $outputStream = $null
        $inputStream.Dispose()
        $inputStream = $null

        if (Test-Path -LiteralPath $destination) {
            Remove-Item -LiteralPath $partial -Force
            [pscustomobject]@{ Kind = 'Result'; Success = $true; Skipped = $true; Message = '目标已存在，跳过' }
            return
        }

        [IO.File]::Move($partial, $destination)
        [IO.File]::SetLastWriteTimeUtc($destination, [IO.File]::GetLastWriteTimeUtc($source))
        $averageSpeed = if ($timer.Elapsed.TotalSeconds -gt 0) { ($Item.Length / 1MB) / $timer.Elapsed.TotalSeconds } else { 0 }
        [pscustomobject]@{ Kind = 'Progress'; Bytes = $Item.Length; Percent = 100; Speed = $averageSpeed; Eta = '00:00:00' }
        [pscustomobject]@{ Kind = 'Result'; Success = $true; Skipped = $false; Message = '完成' }
    }
    catch {
        [pscustomobject]@{ Kind = 'Result'; Success = $false; Skipped = $false; Message = $_.Exception.Message }
    }
    finally {
        if ($null -ne $outputStream) { $outputStream.Dispose() }
        if ($null -ne $inputStream) { $inputStream.Dispose() }
    }
}

$active = @{}
$nextIndex = 0
$completed = 0
$failed = 0
$totalToCopy = $pending.Count

function Start-NextCopy([int]$Slot) {
    if ($script:nextIndex -ge $pending.Count) { return $false }
    $item = $pending[$script:nextIndex]
    $number = $script:nextIndex + 1
    $job = Start-Job -ScriptBlock $copyWorker -ArgumentList $item
    $active[$Slot] = [pscustomobject]@{
        Job = $job; Item = $item; Number = $number
        Percent = 0.0; Status = '准备开始...'; Result = $null
    }
    $script:nextIndex++
    return $true
}

$interactive = $false
try { $interactive = -not [Console]::IsOutputRedirected } catch { $interactive = $false }
$dashboardTop = 0
if ($interactive) {
    Write-Host ''
    for ($slot = 1; $slot -le $Jobs; $slot++) { Write-Host '' }
    $dashboardTop = [Console]::CursorTop - $Jobs
}

function Draw-Dashboard {
    if (-not $interactive) { return }
    $width = [Math]::Max(20, [Console]::WindowWidth - 1)
    for ($slot = 1; $slot -le $Jobs; $slot++) {
        if ($active.ContainsKey($slot)) {
            $entry = $active[$slot]
            $line = '[{0}/{1}] {2} | {3}' -f $entry.Number, $totalToCopy, $entry.Item.Name, $entry.Status
        } else {
            $line = ''
        }
        if ($line.Length -gt $width) { $line = $line.Substring(0, $width) }
        [Console]::SetCursorPosition(0, $dashboardTop + $slot - 1)
        [Console]::Write($line.PadRight($width))
    }
}

for ($slot = 1; $slot -le $Jobs; $slot++) {
    if (-not (Start-NextCopy $slot)) { break }
}

while ($active.Count -gt 0) {
    foreach ($slot in @($active.Keys)) {
        $entry = $active[$slot]
        $events = @(Receive-Job -Job $entry.Job)
        foreach ($event in $events) {
            if ($event.Kind -eq 'Progress') {
                $entry.Percent = [double]$event.Percent
                $entry.Status = '{0:N1}% | {1:N2} MB/s | 剩余 {2}' -f $event.Percent, $event.Speed, $event.Eta
            } elseif ($event.Kind -eq 'Result') {
                $entry.Result = $event
            }
        }
    }

    Draw-Dashboard

    foreach ($slot in @($active.Keys)) {
        $entry = $active[$slot]
        if ($entry.Job.State -in @('Running', 'NotStarted')) { continue }

        $events = @(Receive-Job -Job $entry.Job)
        foreach ($event in $events) {
            if ($event.Kind -eq 'Result') { $entry.Result = $event }
        }

        if ($null -eq $entry.Result -or -not $entry.Result.Success) {
            $failed++
            $message = if ($null -ne $entry.Result) { $entry.Result.Message } else { $entry.Job.State }
            $entry.Status = "失败：$message"
        } else {
            $entry.Status = $entry.Result.Message
        }
        $completed++

        if (-not $interactive) {
            Write-Host ('[{0}/{1}] {2} | {3}' -f $entry.Number, $totalToCopy, $entry.Item.Name, $entry.Status)
        }

        Remove-Job -Job $entry.Job -Force
        $active.Remove($slot)
        [void](Start-NextCopy $slot)
    }

    if ($active.Count -gt 0) { Start-Sleep -Milliseconds 500 }
}

if ($interactive) {
    Draw-Dashboard
    [Console]::SetCursorPosition(0, $dashboardTop + $Jobs)
    Write-Host ''
}

if ($failed -eq 0) {
    Write-Host ('复制完成：成功 {0} 个，失败 0 个，共 {1:N2} GiB。' -f $totalToCopy, ($totalBytes / 1GB))
    exit 0
}

Write-Host "复制结束：完成 $completed 个，失败 $failed 个。" -ForegroundColor Red
exit 1
