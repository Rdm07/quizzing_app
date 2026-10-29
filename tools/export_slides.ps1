# Exports every slide of a deck to PNG (Slide1.PNG, Slide2.PNG ...) using the installed PowerPoint.
param(
    [Parameter(Mandatory=$true)][string]$Deck,
    [Parameter(Mandatory=$true)][string]$OutDir,
    [int]$Width = 1920
)
$ErrorActionPreference = 'Stop'
$Deck = (Resolve-Path $Deck).Path
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
$app = New-Object -ComObject PowerPoint.Application
try {
    # ReadOnly, Untitled, WithWindow=false
    $pres = $app.Presentations.Open($Deck, -1, 0, 0)
    $ratio = $pres.PageSetup.SlideHeight / $pres.PageSetup.SlideWidth
    $height = [int]($Width * $ratio)
    $pres.Export($OutDir, "PNG", $Width, $height)
    $pres.Close()
} finally {
    $app.Quit()
}
Write-Output "Exported to $OutDir"
