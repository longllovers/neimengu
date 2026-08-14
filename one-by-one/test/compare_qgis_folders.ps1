[CmdletBinding()]
param(
    [Parameter()]
    [string]$LeftDir = 'E:\qgis',

    [Parameter()]
    [string]$RightDir = 'E:\qgis_1',

    [Parameter()]
    [string]$OutputDir = (Join-Path $PSScriptRoot '文件比较结果')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-FileMap {
    param(
        [Parameter(Mandatory)]
        [string]$Root
    )

    $map = [System.Collections.Generic.Dictionary[string, System.IO.FileInfo]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )

    foreach ($file in Get-ChildItem -LiteralPath $Root -File -Recurse -Force) {
        $relativePath = [System.IO.Path]::GetRelativePath($Root, $file.FullName)
        $map[$relativePath] = $file
    }

    return $map
}

if (-not (Test-Path -LiteralPath $LeftDir -PathType Container)) {
    throw "左侧目录不存在：$LeftDir"
}
if (-not (Test-Path -LiteralPath $RightDir -PathType Container)) {
    throw "右侧目录不存在：$RightDir"
}

$leftRoot = (Resolve-Path -LiteralPath $LeftDir).Path.TrimEnd('\')
$rightRoot = (Resolve-Path -LiteralPath $RightDir).Path.TrimEnd('\')

if ($leftRoot -eq $rightRoot) {
    throw '两个目录指向同一个位置，无需比较。'
}

Write-Host "正在扫描：$leftRoot"
$leftFiles = Get-FileMap -Root $leftRoot
Write-Host "正在扫描：$rightRoot"
$rightFiles = Get-FileMap -Root $rightRoot

$allRelativePaths = @(
    @(
        $leftFiles.Keys
        $rightFiles.Keys
    ) | Sort-Object -Unique
)

$results = [System.Collections.Generic.List[object]]::new()
$index = 0

foreach ($relativePath in $allRelativePaths) {
    $index++
    Write-Progress `
        -Activity '正在比较文件' `
        -Status "$index / $($allRelativePaths.Count)：$relativePath" `
        -PercentComplete (($index / [Math]::Max(1, $allRelativePaths.Count)) * 100)

    $hasLeft = $leftFiles.ContainsKey($relativePath)
    $hasRight = $rightFiles.ContainsKey($relativePath)

    if (-not $hasLeft) {
        $rightFile = $rightFiles[$relativePath]
        $results.Add([pscustomobject]@{
            '状态'       = '仅qgis_1存在'
            '相对路径'   = $relativePath
            'qgis大小'   = $null
            'qgis_1大小' = $rightFile.Length
            'qgis哈希'   = $null
            'qgis_1哈希' = $null
            '说明'       = 'E:\qgis 中没有此文件'
        })
        continue
    }

    if (-not $hasRight) {
        $leftFile = $leftFiles[$relativePath]
        $results.Add([pscustomobject]@{
            '状态'       = '仅qgis存在'
            '相对路径'   = $relativePath
            'qgis大小'   = $leftFile.Length
            'qgis_1大小' = $null
            'qgis哈希'   = $null
            'qgis_1哈希' = $null
            '说明'       = 'E:\qgis_1 中没有此文件'
        })
        continue
    }

    $leftFile = $leftFiles[$relativePath]
    $rightFile = $rightFiles[$relativePath]

    if ($leftFile.Length -ne $rightFile.Length) {
        $results.Add([pscustomobject]@{
            '状态'       = '内容不同'
            '相对路径'   = $relativePath
            'qgis大小'   = $leftFile.Length
            'qgis_1大小' = $rightFile.Length
            'qgis哈希'   = $null
            'qgis_1哈希' = $null
            '说明'       = '文件大小不同'
        })
        continue
    }

    try {
        $leftHash = (Get-FileHash -LiteralPath $leftFile.FullName -Algorithm SHA256).Hash
        $rightHash = (Get-FileHash -LiteralPath $rightFile.FullName -Algorithm SHA256).Hash

        if ($leftHash -ne $rightHash) {
            $results.Add([pscustomobject]@{
                '状态'       = '内容不同'
                '相对路径'   = $relativePath
                'qgis大小'   = $leftFile.Length
                'qgis_1大小' = $rightFile.Length
                'qgis哈希'   = $leftHash
                'qgis_1哈希' = $rightHash
                '说明'       = '文件大小相同，但SHA-256不同'
            })
        }
    }
    catch {
        $results.Add([pscustomobject]@{
            '状态'       = '读取失败'
            '相对路径'   = $relativePath
            'qgis大小'   = $leftFile.Length
            'qgis_1大小' = $rightFile.Length
            'qgis哈希'   = $null
            'qgis_1哈希' = $null
            '说明'       = $_.Exception.Message
        })
    }
}

Write-Progress -Activity '正在比较文件' -Completed

$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$reportDir = Join-Path $OutputDir $timestamp
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null

$sortedResults = @($results | Sort-Object '状态', '相对路径')
$onlyLeft = @($sortedResults | Where-Object '状态' -eq '仅qgis存在')
$onlyRight = @($sortedResults | Where-Object '状态' -eq '仅qgis_1存在')
$contentDifferent = @($sortedResults | Where-Object '状态' -eq '内容不同')
$readFailures = @($sortedResults | Where-Object '状态' -eq '读取失败')

$sortedResults | Export-Csv `
    -LiteralPath (Join-Path $reportDir '全部差异.csv') `
    -NoTypeInformation `
    -Encoding utf8BOM
$onlyLeft | Export-Csv `
    -LiteralPath (Join-Path $reportDir '仅qgis存在.csv') `
    -NoTypeInformation `
    -Encoding utf8BOM
$onlyRight | Export-Csv `
    -LiteralPath (Join-Path $reportDir '仅qgis_1存在.csv') `
    -NoTypeInformation `
    -Encoding utf8BOM
$contentDifferent | Export-Csv `
    -LiteralPath (Join-Path $reportDir '内容不同.csv') `
    -NoTypeInformation `
    -Encoding utf8BOM
$readFailures | Export-Csv `
    -LiteralPath (Join-Path $reportDir '读取失败.csv') `
    -NoTypeInformation `
    -Encoding utf8BOM

$sameCount = $allRelativePaths.Count - $sortedResults.Count
$summary = @"
文件夹比较结果
==============
左侧目录：$leftRoot
右侧目录：$rightRoot
比较时间：$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')

qgis 文件数：$($leftFiles.Count)
qgis_1 文件数：$($rightFiles.Count)
内容相同：$sameCount
仅 qgis 存在：$($onlyLeft.Count)
仅 qgis_1 存在：$($onlyRight.Count)
内容不同：$($contentDifferent.Count)
读取失败：$($readFailures.Count)
差异合计：$($sortedResults.Count)
"@

$summary | Set-Content `
    -LiteralPath (Join-Path $reportDir '汇总.txt') `
    -Encoding utf8BOM

Write-Host ''
Write-Host $summary
Write-Host "结果已保存到：$reportDir"

if ($sortedResults.Count -eq 0) {
    Write-Host '结论：两个文件夹中的文件完全一致。' -ForegroundColor Green
}
else {
    Write-Host '结论：两个文件夹中的文件不一致，请查看输出结果。' -ForegroundColor Yellow
    $sortedResults |
        Select-Object '状态', '相对路径', '说明' |
        Format-Table -AutoSize
}
