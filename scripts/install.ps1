# Install the standalone Aria Code CLI without Python, Node.js, or npm.
$ErrorActionPreference = 'Stop'

$arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
if ($arch -ne 'X64') { throw "Unsupported Windows architecture: $arch (the release currently ships Windows x64 only)." }

$version = if ($env:ARIA_CODE_VERSION) { $env:ARIA_CODE_VERSION } else { 'latest' }
if ($version -eq 'latest') {
    $base = 'https://github.com/artheras/aria-code/releases/latest/download'
} elseif ($version -match '^v\d+\.\d+\.\d+$') {
    $base = "https://github.com/artheras/aria-code/releases/download/$version"
} else {
    throw 'ARIA_CODE_VERSION must look like v0.55.0.'
}

# Releases ship a PyInstaller --onedir build as aria-code-windows-x64.zip: the
# executable plus an _internal folder it loads from. --onefile unpacked every
# library to a new temp folder on each launch. Releases before the switch have
# a single aria-code-windows-x64.exe, which this still installs.
$asset = 'aria-code-windows-x64'
$installDir = if ($env:ARIA_CODE_INSTALL_DIR) { $env:ARIA_CODE_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA 'AriaCode\bin' }
$tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("aria-code-install-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempDir | Out-Null

function Get-Checksum([string[]]$lines, [string]$name) {
    $line = $lines | Where-Object { $_ -match "^[a-fA-F0-9]{64}\s+\*?$([regex]::Escape($name))$" } | Select-Object -First 1
    if ($line) { return ($line -split '\s+')[0].ToLowerInvariant() }
    return $null
}

try {
    $checksums = Join-Path $tempDir 'SHA256SUMS'
    Invoke-WebRequest -UseBasicParsing -Uri "$base/SHA256SUMS" -OutFile $checksums
    $lines = Get-Content $checksums

    $file = "$asset.zip"
    $expected = Get-Checksum $lines $file
    if (-not $expected) {
        $file = "$asset.exe"
        $expected = Get-Checksum $lines $file
    }
    if (-not $expected) { throw "This release has no Windows x64 build." }

    $download = Join-Path $tempDir $file
    Write-Host "Downloading $file..."
    Invoke-WebRequest -UseBasicParsing -Uri "$base/$file" -OutFile $download
    $actual = (Get-FileHash -Algorithm SHA256 -Path $download).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw "Checksum mismatch for $file." }

    New-Item -ItemType Directory -Force -Path $installDir | Out-Null
    $destination = Join-Path $installDir 'aria-code.exe'
    $libraries = Join-Path $installDir '_internal'
    $stage = Join-Path $installDir ('.aria-code-stage-' + [guid]::NewGuid().ToString('N'))
    $backup = Join-Path $installDir ('.aria-code-backup-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage | Out-Null

    if ($file -like '*.exe') {
        & $download --version | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Downloaded binary failed its version check.' }
        Copy-Item $download (Join-Path $stage 'aria-code.exe')
    } else {
        $unpacked = Join-Path $tempDir 'unpacked'
        Expand-Archive -Path $download -DestinationPath $unpacked
        $build = Join-Path $unpacked 'aria-code-bin'
        $exe = Join-Path $build 'aria-code-bin.exe'
        if (-not (Test-Path $exe)) { throw "$file does not contain aria-code-bin\aria-code-bin.exe." }
        & $exe --version | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Downloaded binary failed its version check.' }

        # _internal is a generic name other PyInstaller apps use too. Only
        # replace one that sits beside an aria-code.exe.
        if ((Test-Path $libraries) -and -not (Test-Path $destination)) {
            throw "$installDir already has an _internal folder that is not Aria Code's; set ARIA_CODE_INSTALL_DIR to an empty folder."
        }
        Copy-Item -Recurse (Join-Path $build '_internal') (Join-Path $stage '_internal')
        Copy-Item $exe (Join-Path $stage 'aria-code.exe')
    }
    Copy-Item (Join-Path $stage 'aria-code.exe') (Join-Path $stage 'aria.exe')

    # Stage everything before touching the installed build. Keep the old files
    # until the replacement completes, and restore them if a move fails (for
    # example because Windows has the old executable open).
    New-Item -ItemType Directory -Path $backup | Out-Null
    $names = @('aria-code.exe', 'aria.exe', '_internal')
    $movedNew = @()
    try {
        foreach ($name in $names) {
            $current = Join-Path $installDir $name
            if (Test-Path $current) { Move-Item $current (Join-Path $backup $name) }
        }
        foreach ($name in $names) {
            $prepared = Join-Path $stage $name
            if (Test-Path $prepared) {
                Move-Item $prepared (Join-Path $installDir $name)
                $movedNew += $name
            }
        }
    } catch {
        foreach ($name in $names) {
            $current = Join-Path $installDir $name
            $previous = Join-Path $backup $name
            if (Test-Path $previous) {
                if (Test-Path $current) { Remove-Item -Recurse -Force $current }
                Move-Item $previous $current
            } elseif (($movedNew -contains $name) -and (Test-Path $current)) {
                Remove-Item -Recurse -Force $current
            }
        }
        throw
    } finally {
        Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
    }
    Remove-Item -Recurse -Force $backup

    if (-not $env:ARIA_CODE_INSTALL_DIR) {
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        $entries = @($userPath -split ';' | Where-Object { $_ })
        if ($entries -notcontains $installDir) {
            [Environment]::SetEnvironmentVariable('Path', (($entries + $installDir) -join ';'), 'User')
            Write-Host 'Added AriaCode\bin to your user PATH for future terminals.'
        }
        $env:Path = "$installDir;$env:Path"
    }
    Write-Host "Installed $destination"
    Write-Host "Run it now: & '$destination' --help"
} finally {
    if ($stage -and (Test-Path $stage)) {
        Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
    }
    Remove-Item -Recurse -Force $tempDir -ErrorAction SilentlyContinue
}
