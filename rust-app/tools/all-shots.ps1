# Takes the four native screenshots that pair with ref-shot.mjs, and writes the comparison sheets.
#   pwsh all-shots.ps1 -Exe <LawKitty.exe> -Data <data folder> -Ref <folder of reference pngs> -Out <folder> [-Theme light]
param(
    [Parameter(Mandatory)] [string]$Exe,
    [Parameter(Mandatory)] [string]$Data,
    [Parameter(Mandatory)] [string]$Ref,
    [Parameter(Mandatory)] [string]$Out,
    [string]$Theme = 'dark',
    [string[]]$Only = @('home', 'tree', 'section', 'search')
)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
New-Item -ItemType Directory -Force $Out | Out-Null
# A scratch settings folder, so the pictures do not depend on (or change) the user's pins.
$state = Join-Path $env:TEMP 'lawkitty-shots-state'
Remove-Item -Recurse -Force $state -ErrorAction SilentlyContinue
$views = [ordered]@{ home = @(); tree = @('--open', 'itaa-1997'); section = @('--open', 'itaa-1997/8-1'); search = @('--open', 'search:') }
foreach ($name in $views.Keys) {
    if ($Only -notcontains $name) { continue }
    $args = @('--data', $Data, '--zoom', '1', '--theme', $Theme, '--state-dir', $state) + $views[$name]
    $suffix = if ($Theme -eq 'dark') { '' } else { "-$Theme" }
    & "$here\shot.ps1" -Exe $Exe -Out "$Out\$name$suffix-native.png" -AppArgs $args -Wait 2400
    if (Test-Path "$Ref\$name.png") {
        Copy-Item "$Ref\$name.png" "$Out\$name$suffix-web.png" -Force
        py "$here\compare.py" "$Out\$name$suffix-web.png" "$Out\$name$suffix-native.png" "$Out\$name$suffix-compare.png"
    }
}
