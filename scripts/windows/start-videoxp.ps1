# Opens Video XP (GitHub Pages) in its own Brave window (Brave's shields block YouTube's ads): no tabs, no address bar.
# Run by the "Video XP" Desktop shortcut through start-videoxp.vbs, so no console window flashes.
# Falls back to Chrome / Edge (with ads) only if Brave isn't installed.
$Url = 'https://atheendre130505.github.io/video-xp/'
foreach ($p in @("$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe",
                 "${env:ProgramFiles(x86)}\BraveSoftware\Brave-Browser\Application\brave.exe",
                 "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\Application\brave.exe",
                 "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
                 "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
                 "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
                 "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
                 "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe")) {
  if (Test-Path $p) { Start-Process $p "--app=$Url --start-maximized"; exit 0 }
}
Start-Process $Url
