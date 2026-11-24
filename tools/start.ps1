param(
    [ValidateRange(1, 65535)][int]$WebPort = 5173,
    [ValidateRange(1, 65535)][int]$CoachPort = 5188
)

$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $workspace '.xiyun/llama-cpu/llama-server.exe'
$model = Join-Path $workspace '.xiyun/models/Qwen3-0.6B-Q4_0.gguf'
$key = Join-Path $workspace '.xiyun/models/coach.key'
if (!(Test-Path -LiteralPath $runner) -or !(Test-Path -LiteralPath $model)) {
    throw '本机 AI 缺少 llama-server 或 Qwen3 GGUF。请参考曲库评测文档。'
}
if (Get-NetTCPConnection -LocalPort $CoachPort -State Listen -ErrorAction SilentlyContinue) {
    throw "端口 $CoachPort 已有服务，请使用 -CoachPort 指定空闲端口。"
}
if (!(Test-Path -LiteralPath $key)) {
    [System.IO.File]::WriteAllText($key, [Guid]::NewGuid().ToString('N'))
}

Push-Location -LiteralPath $workspace
$coachProcess = $null
$previousCoachPort = $env:XIYUN_COACH_PORT
try {
    $env:XIYUN_COACH_PORT = "$CoachPort"
    $processOptions = @{
        FilePath = $runner
        WorkingDirectory = $workspace
        WindowStyle = 'Hidden'
        PassThru = $true
        ArgumentList = @('-m', '.xiyun/models/Qwen3-0.6B-Q4_0.gguf', '--host', '127.0.0.1', '--port',
            "$CoachPort", '-c', '3072', '-t', '6', '-np', '1', '--no-webui', '--api-key-file', '.xiyun/models/coach.key')
        RedirectStandardOutput = Join-Path $workspace '.xiyun/coach-out.log'
        RedirectStandardError = Join-Path $workspace '.xiyun/coach-error.log'
    }
    $coachProcess = Start-Process @processOptions
    py -3.12 -X utf8 -m server --port $WebPort
} finally {
    if ($coachProcess -and !$coachProcess.HasExited) { Stop-Process -Id $coachProcess.Id }
    $env:XIYUN_COACH_PORT = $previousCoachPort
    Pop-Location
}
