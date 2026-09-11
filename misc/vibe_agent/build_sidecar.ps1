param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
if (-not $PythonExe) {
    $PythonExe = if ($env:VIBE_BUILD_PYTHON) { $env:VIBE_BUILD_PYTHON } else { (Get-Command python -ErrorAction Stop).Source }
}
$libraryRoot = Join-Path $repoRoot ".build/pythonlibs"
if (-not (Test-Path -LiteralPath (Join-Path $libraryRoot "PyInstaller"))) {
    throw "PyInstaller is missing from .build/pythonlibs. Install build dependencies first."
}

$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = "$libraryRoot;$repoRoot"
$dist = Join-Path $repoRoot ".build/sidecar-dist"
$work = Join-Path $repoRoot ".build/sidecar-work"
$spec = Join-Path $repoRoot ".build/sidecar-spec"
New-Item -ItemType Directory -Path $dist, $work, $spec -Force | Out-Null

try {
    & $PythonExe -m PyInstaller `
        --noconfirm --clean --onedir --windowed `
        --name godotvibe-agent `
        --distpath $dist --workpath $work --specpath $spec `
        --paths $repoRoot --paths $libraryRoot `
        --add-data "$repoRoot/vibe_tools/prompts;vibe_tools/prompts" `
        --add-data "$repoRoot/vibe_tools/api_cache;vibe_tools/api_cache" `
        --collect-submodules vibe_tools.templates `
        --collect-all openai --collect-all uvicorn `
        --exclude-module pandas --exclude-module openpyxl `
        --exclude-module lxml --exclude-module numpy `
        --hidden-import uvicorn.logging `
        --hidden-import uvicorn.loops.auto `
        --hidden-import uvicorn.protocols.http.auto `
        --hidden-import uvicorn.protocols.websockets.auto `
        --hidden-import uvicorn.lifespan.on `
        (Join-Path $repoRoot "vibe_tools/editor_sidecar.py")
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller sidecar build failed."
    }
} finally {
    $env:PYTHONPATH = $previousPythonPath
}

$exe = Join-Path $dist "godotvibe-agent/godotvibe-agent.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "Sidecar executable was not created: $exe"
}
Write-Host "Sidecar created: $exe"
