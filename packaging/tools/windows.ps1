# Collects the CD/DVD tools that ship inside DiscForge's Windows download, from pinned downloads.
#
#   powershell -File packaging/tools/windows.ps1 OUT_DIR [SOURCES_DIR]
#
#   OUT_DIR\bin       cdrecord.exe readcd.exe mkisofs.exe  cdrtools 3.02a09, native 32-bit build
#                                                          (TumaGonx Zakkum; the build Scoop ships)
#                     cdrdao.exe + msys-*.dll              cdrdao 1.2.6, the project's own Windows build
#                     7z.exe 7z.dll                        7-Zip 26.05
#                     chdman.exe                           MAME 0.289's CHD tool
#   OUT_DIR\licenses  every tool's licence and SOURCES.md
#   SOURCES_DIR       the source archives, published next to each release
param(
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$Src = ""
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"            # Invoke-WebRequest is very slow with a progress bar

$Out = [IO.Path]::GetFullPath($Out)
if (-not $Src) { $Src = Join-Path (Split-Path $Out -Parent) "tool-sources" }
$Src = [IO.Path]::GetFullPath($Src)
$Work = Join-Path ([IO.Path]::GetTempPath()) ("discforge-tools-" + [Guid]::NewGuid().ToString("N"))
$Bin = Join-Path $Out "bin"
$Lic = Join-Path $Out "licenses"
$Tar = Join-Path $env:SystemRoot "System32\tar.exe"   # Windows' own bsdtar (not Git's tar on PATH)
New-Item -ItemType Directory -Force -Path $Bin, $Lic, $Src, $Work | Out-Null

function Fetch([string]$Url, [string]$Sha256, [string]$Name, [string]$Dir = $Src) {
    $dest = Join-Path $Dir $Name
    if ((Test-Path $dest) -and ((Get-FileHash $dest -Algorithm SHA256).Hash -eq $Sha256.ToUpper())) {
        return $dest
    }
    Write-Host "==> download $Url"
    for ($i = 1; $i -le 4; $i++) {
        try { Invoke-WebRequest -Uri $Url -OutFile "$dest.part" -UseBasicParsing; break }
        catch { if ($i -eq 4) { throw }; Start-Sleep -Seconds (5 * $i) }
    }
    $got = (Get-FileHash "$dest.part" -Algorithm SHA256).Hash
    if ($got -ne $Sha256.ToUpper()) { throw "checksum mismatch for ${Url}: $got" }
    Move-Item -Force "$dest.part" $dest
    return $dest
}

$Rows = New-Object System.Collections.Generic.List[string]
function Row($Tool, $Version, $Licence, $Url, $Sha) { $Rows.Add("| $Tool | $Version | $Licence | $Url | ``$Sha`` |") }

# ---------------------------------------------------------------------------------------------
Write-Host "==> 7-Zip 26.05"
$msi = Fetch "https://github.com/ip7z/7zip/releases/download/26.05/7z2605-x64.msi" `
    "3fe4ab99fa15629c652944309a2b2bc6e41fc933fc6b63f5df1ebcf3aea68263" "7z2605-x64.msi" $Work
$p = Start-Process msiexec.exe -Wait -PassThru -ArgumentList @("/a", "`"$msi`"", "/qn", "TARGETDIR=`"$Work\7zip`"")
if ($p.ExitCode -ne 0) { throw "msiexec failed ($($p.ExitCode))" }
$sz = Get-ChildItem -Recurse -Path "$Work\7zip" -Filter 7z.exe | Select-Object -First 1
if (-not $sz) { throw "7z.exe isn't in the 7-Zip installer" }
Copy-Item $sz.FullName, (Join-Path $sz.DirectoryName "7z.dll") $Bin
Copy-Item (Join-Path $sz.DirectoryName "License.txt") (Join-Path $Lic "7-Zip-License.txt")
$7z = Join-Path $Bin "7z.exe"
Fetch "https://github.com/ip7z/7zip/releases/download/26.05/7z2605-src.tar.xz" `
    "0b6080b8c3e7a579161d2d495224c0e51c467576cb4c7048b0ab485ef44cc22e" "7z2605-src.tar.xz" | Out-Null
Row "7-Zip" "26.05" "LGPL-2.1-or-later, BSD-3-Clause, unRAR restriction" `
    "https://github.com/ip7z/7zip/releases/download/26.05/7z2605-src.tar.xz" `
    "0b6080b8c3e7a579161d2d495224c0e51c467576cb4c7048b0ab485ef44cc22e"

# ---------------------------------------------------------------------------------------------
Write-Host "==> cdrtools 3.02a09 (cdrecord, readcd, mkisofs)"
$cdr = Fetch "https://downloads.sourceforge.net/project/tumagcc/schily-cdrtools-3.02a09.7z" `
    "0b83f199ebf72cc5a1b60bcb4aa915a420c33b8886b00496e21467e5845aa135" "schily-cdrtools-3.02a09.7z" $Work
& $7z x -y "-o$Work\cdrtools" $cdr | Out-Null
if ($LASTEXITCODE -ne 0) { throw "couldn't unpack cdrtools" }
foreach ($t in "cdrecord", "readcd", "mkisofs") { Copy-Item "$Work\cdrtools\win32\$t.exe" $Bin }
$patch = "cdrtools-3.02a01_mingw.patch"
Copy-Item "$Work\cdrtools\$patch" (Join-Path $Src $patch)
$cdrsrc = Fetch "https://downloads.sourceforge.net/project/cdrtools/alpha/cdrtools-3.02a09.tar.bz2" `
    "aa28438f458ef3f314b79f2029db27679dae1d5ffe1569b6de57742511915e81" "cdrtools-3.02a09.tar.bz2"
New-Item -ItemType Directory -Force -Path "$Work\cdrsrc" | Out-Null
& $Tar -xjf $cdrsrc -C "$Work\cdrsrc" cdrtools-3.02/COPYING cdrtools-3.02/CDDL.Schily.txt `
    cdrtools-3.02/GPL-2.0.txt cdrtools-3.02/LGPL-2.1.txt
if ($LASTEXITCODE -ne 0) { throw "couldn't read the cdrtools licences" }
Copy-Item "$Work\cdrsrc\cdrtools-3.02\COPYING" "$Lic\cdrtools-COPYING.txt"
Copy-Item "$Work\cdrsrc\cdrtools-3.02\CDDL.Schily.txt" "$Lic\cdrtools-CDDL.txt"
Copy-Item "$Work\cdrsrc\cdrtools-3.02\GPL-2.0.txt" "$Lic\cdrtools-GPL-2.0.txt"
Copy-Item "$Work\cdrsrc\cdrtools-3.02\LGPL-2.1.txt" "$Lic\cdrtools-LGPL-2.1.txt"
Row "cdrtools" "3.02a09 + MinGW patch (in the source bundle)" "CDDL-1.0 (cdrecord, readcd), GPL-2.0 (mkisofs)" `
    "https://downloads.sourceforge.net/project/cdrtools/alpha/cdrtools-3.02a09.tar.bz2" `
    "aa28438f458ef3f314b79f2029db27679dae1d5ffe1569b6de57742511915e81"

# ---------------------------------------------------------------------------------------------
Write-Host "==> cdrdao 1.2.6"
$zip = Fetch "https://github.com/cdrdao/cdrdao/releases/download/rel_1_2_6/cdrdao126.zip" `
    "653e56c49f94771f87cd9f4ceefc255329714ba5066bd78e30edaa464c322a3a" "cdrdao126.zip" $Work
Expand-Archive -Force $zip "$Work\cdrdao"
Copy-Item "$Work\cdrdao\cdrdao.exe" $Bin
Copy-Item "$Work\cdrdao\msys-*.dll" $Bin
$daosrc = Fetch "https://github.com/cdrdao/cdrdao/releases/download/rel_1_2_6/cdrdao-1.2.6.tar.bz2" `
    "0cf29e1183ffd8e4d167c403d7a6ea2109a2dd4cd97e61f80417cf1373220ff4" "cdrdao-1.2.6.tar.bz2"
New-Item -ItemType Directory -Force -Path "$Work\daosrc" | Out-Null
& $Tar -xjf $daosrc -C "$Work\daosrc" cdrdao-1.2.6/COPYING
if ($LASTEXITCODE -ne 0) { throw "couldn't read the cdrdao licence" }
Copy-Item "$Work\daosrc\cdrdao-1.2.6\COPYING" "$Lic\cdrdao-COPYING.txt"
Row "cdrdao" "1.2.6 (the project's MSYS2 build)" "GPL-2.0-or-later" `
    "https://github.com/cdrdao/cdrdao/releases/download/rel_1_2_6/cdrdao-1.2.6.tar.bz2" `
    "0cf29e1183ffd8e4d167c403d7a6ea2109a2dd4cd97e61f80417cf1373220ff4"
Row "MSYS2 runtime DLLs (with cdrdao)" "from cdrdao126.zip" "LGPL-3.0-or-later; GCC runtime exception; LGPL-2.1 (iconv)" `
    "https://github.com/msys2/msys2-runtime and https://github.com/msys2/MSYS2-packages" `
    "653e56c49f94771f87cd9f4ceefc255329714ba5066bd78e30edaa464c322a3a (cdrdao126.zip)"

# ---------------------------------------------------------------------------------------------
Write-Host "==> chdman (MAME 0.289)"
$mame = Fetch "https://github.com/mamedev/mame/releases/download/mame0289/mame0289b_x64.exe" `
    "a1aa7912168c9d1b05e611906bc21b8b9be3935822aead36d12a1da363150b7d" "mame0289b_x64.exe" $Work
& $7z e -y "-o$Work\mame" $mame chdman.exe COPYING | Out-Null
if (-not (Test-Path "$Work\mame\chdman.exe")) { throw "chdman.exe isn't in the MAME package" }
Copy-Item "$Work\mame\chdman.exe" $Bin
if (Test-Path "$Work\mame\COPYING") { Copy-Item "$Work\mame\COPYING" "$Lic\MAME-COPYING.txt" }
else { Copy-Item "$Lic\cdrtools-GPL-2.0.txt" "$Lic\MAME-GPL-2.0.txt" }
Row "chdman (MAME)" "0.289" "GPL-2.0-or-later" `
    "https://github.com/mamedev/mame/releases/download/mame0289/mame0289s.exe" `
    "6243eac3a80df6be602bf0af7889305691737bc26c2ded1433ec11b7c6a3c7b9"

# ---------------------------------------------------------------------------------------------
$header = @"
# Where DiscForge's bundled tools come from

DiscForge runs these programs as separate processes. Each keeps its own licence (see the other
files in this folder). Their source archives are published with every DiscForge release
(``DiscForge-<version>-tool-sources.tar``) and are listed here with their SHA-256 checksums.

| Tool | Version | Licence | Source | SHA-256 |
|---|---|---|---|---|
"@
Set-Content -Encoding utf8 -Path (Join-Path $Lic "SOURCES.md") -Value ($header + "`n" + ($Rows -join "`n") + "`n")

Write-Host "==> checking the tools start"
$ErrorActionPreference = "Continue"                   # these print their banners on stderr
$checks = @{
    "cdrecord.exe" = @("-version"); "readcd.exe" = @("-version"); "mkisofs.exe" = @("-version")
    "cdrdao.exe" = @("show-toc", "--help"); "7z.exe" = @("i"); "chdman.exe" = @()
}
$want = @{ "cdrecord.exe" = "Cdrecord"; "readcd.exe" = "readcd"; "mkisofs.exe" = "mkisofs";
           "cdrdao.exe" = "cdrdao version"; "7z.exe" = "7-Zip"; "chdman.exe" = "chdman" }
foreach ($exe in $checks.Keys) {
    $text = (& (Join-Path $Bin $exe) @($checks[$exe]) 2>&1 | Out-String)
    $first = ($text -split "`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
    Write-Host ("  {0,-13} {1}" -f $exe, $first)
    if ($text -notmatch [regex]::Escape($want[$exe])) { throw "$exe doesn't run: $text" }
}
$ErrorActionPreference = "Stop"
Remove-Item -Recurse -Force $Work
Get-ChildItem $Bin | Format-Table Name, Length
