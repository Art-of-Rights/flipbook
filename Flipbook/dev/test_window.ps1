<#
  End-to-end test of the About window (manual + version history) and the keys, inside a REAL
  Flipbook.exe window (v1.8.x). Run it on a COPY of the built exe in a scratch folder:

    powershell -ExecutionPolicy Bypass -File Flipbook\dev\test_window.ps1 -Exe <scratch>\Flipbook.exe `
        -Pdf "<some.pdf>" -Out <scratch>\shots -Version 1.8.5

  How: the exe is started with WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9333
  and LOCALAPPDATA pointed at <Out>\localappdata, so it has its OWN WebView profile (the user's
  open Flipbook and settings are never touched). It is driven over the DevTools protocol with
  System.Net.WebSockets.ClientWebSocket: real clicks/keys (Input.dispatch*), checks (Runtime.evaluate)
  and screenshots (Page.captureScreenshot, saved to -Out). Prints PASS/FAIL lines; closes only its
  own processes. Delete <Out>\localappdata afterwards.
#>
param([string]$Exe, [string]$Pdf, [string]$Out, [string]$Version = "1.8.5", [int]$LinkPage = 0)
# -LinkPage N (v1.8.8): page N of the book is a printed contents page; its lines are clicked as page links
# (the file page of a printed contents page). Without it, only the page-link keys and the switch are checked.
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force $Out | Out-Null
$before = @((Get-Process Flipbook -ErrorAction SilentlyContinue).Id)
$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = "--remote-debugging-port=9333"
# own WebView profile (Flipbook keeps it in %LOCALAPPDATA%\Flipbook), so the user's open Flipbook and settings are untouched
$realLad = $env:LOCALAPPDATA
$env:LOCALAPPDATA = Join-Path $Out "localappdata"
Start-Process -FilePath $Exe -ArgumentList "`"$Pdf`""
$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = $null
$env:LOCALAPPDATA = $realLad
$p = $null
for ($i = 0; $i -lt 60 -and -not $p; $i++) {
  Start-Sleep 1
  try { $p = (Invoke-RestMethod "http://127.0.0.1:9333/json") | Where-Object { $_.type -eq "page" -and $_.url -like "*flipbook.html*" } | Select-Object -First 1 } catch {}
}
if (-not $p) { throw "no DevTools page" }
"page: $($p.url)"
$sock = New-Object System.Net.WebSockets.ClientWebSocket
$ct = [Threading.CancellationToken]::None
$sock.ConnectAsync([Uri]$p.webSocketDebuggerUrl, $ct).Wait()
$script:reqId = 0
function Cdp($method, $params) {
  $script:reqId++
  $msg = @{ id = $script:reqId; method = $method; params = $params } | ConvertTo-Json -Depth 10 -Compress
  $b = [Text.Encoding]::UTF8.GetBytes($msg)
  $sock.SendAsync((New-Object 'ArraySegment[byte]' (, $b)), [Net.WebSockets.WebSocketMessageType]::Text, $true, $ct).Wait()
  while ($true) {
    $ms = New-Object IO.MemoryStream; $buf = New-Object byte[] 262144
    do { $r = $sock.ReceiveAsync((New-Object 'ArraySegment[byte]' (, $buf)), $ct).Result; $ms.Write($buf, 0, $r.Count) } while (-not $r.EndOfMessage)
    $txt = [Text.Encoding]::UTF8.GetString($ms.ToArray())
    if ($txt -match ('^\{"id":' + $script:reqId + '[,}]')) {
      Add-Type -AssemblyName System.Web.Extensions
      $js = New-Object Web.Script.Serialization.JavaScriptSerializer; $js.MaxJsonLength = 100MB
      return $js.DeserializeObject($txt)
    }
  }
}
function Ev($code) {
  # allowUnsafeEvalBlockedByCSP: DevTools would otherwise exempt our checks from the page's own eval ban (v1.8.6)
  $r = Cdp "Runtime.evaluate" @{ expression = $code; awaitPromise = $true; returnByValue = $true; allowUnsafeEvalBlockedByCSP = $false }
  if ($r.result.exceptionDetails) { throw ("JS error: " + $r.result.exceptionDetails.exception.description) }
  return $r.result.result.value
}
function Click($pt) {
  if (-not $pt) { throw "element not found" }
  Cdp "Input.dispatchMouseEvent" @{ type = "mouseMoved"; x = $pt.x; y = $pt.y } | Out-Null
  Cdp "Input.dispatchMouseEvent" @{ type = "mousePressed"; x = $pt.x; y = $pt.y; button = "left"; clickCount = 1 } | Out-Null
  Cdp "Input.dispatchMouseEvent" @{ type = "mouseReleased"; x = $pt.x; y = $pt.y; button = "left"; clickCount = 1 } | Out-Null
}
function Key($key, $code, $vk, $text, $mods = 0) {
  $d = @{ type = "keyDown"; key = $key; code = $code; windowsVirtualKeyCode = $vk; modifiers = $mods }
  if ($text) { $d.text = $text }
  Cdp "Input.dispatchKeyEvent" $d | Out-Null
  Cdp "Input.dispatchKeyEvent" @{ type = "keyUp"; key = $key; code = $code; windowsVirtualKeyCode = $vk; modifiers = $mods } | Out-Null
}
function MouseTo($pt) { Cdp "Input.dispatchMouseEvent" @{ type = "mouseMoved"; x = $pt.x; y = $pt.y } | Out-Null }
function At($sel) { Ev "(() => { const e = $sel; if (!e) return null; e.scrollIntoView({block:'nearest', behavior:'instant'}); const r = e.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; })()" }
function Shot($name) { $r = Cdp "Page.captureScreenshot" @{ format = "jpeg"; quality = 70 }; [IO.File]::WriteAllBytes("$Out\$name.jpg", [Convert]::FromBase64String($r.result.data)) }
$results = New-Object System.Collections.ArrayList
function Check($name, $ok, $detail) { [void]$results.Add(("{0}  {1}  {2}" -f ($(if ($ok) { "PASS" } else { "FAIL" })), $name, $detail)) }
function WaitFor($code, $secs) { $t0 = Get-Date; while (((Get-Date) - $t0).TotalSeconds -lt $secs) { if (Ev $code) { return $true }; Start-Sleep -Milliseconds 200 }; return $false }

try {
  Cdp "Page.bringToFront" @{} | Out-Null
  Check "book opened" (WaitFor "!!(document.getElementById('counter') && document.getElementById('counter').textContent.trim())" 30) (Ev "document.getElementById('counter').textContent")
  Check "version" ((Ev "fetch('api/info').then(r => r.json()).then(j => j.version)") -eq $Version) ""
  $theme0 = Ev "document.documentElement.getAttribute('data-theme') || ''"
  Start-Sleep 1

  # Commands (real click on the tools-bar button) -> About button
  Click (At "document.getElementById('cmdsBtn')")
  Check "Commands opens" (WaitFor "document.getElementById('raDlg').style.display === 'flex' && !!document.querySelector('#raDlgBox .cmds')" 5) ""
  $btns = Ev "[...document.querySelectorAll('#raDlgBox .row button')].map(b => b.textContent).join(' | ')"
  Check "Commands window has no About button" ($btns -notlike "*About*") $btns
  # v1.8.10: Add-ons is in the tools bar only, not in the Commands window (the user's wish)
  Check "Commands window has no Add-ons (only Close)" ($btns -eq "Close" -and -not (Ev "/Add-ons/.test(document.getElementById('raDlgBox').textContent)")) $btns
  # v1.8.11: the Read aloud voice menu lists only voices (adding/removing voices is in Add-ons only)
  WaitFor "document.getElementById('raVoice').options.length > 0 && /^(ai|qw|sys):/.test(document.getElementById('raVoice').options[0].value)" 10 | Out-Null
  $vm = Ev "(() => { const s = document.getElementById('raVoice'); return { n: s.options.length, bad: [...s.options].filter(o => !/^(ai|qw|sys):/.test(o.value)).map(o => o.textContent), groups: [...s.querySelectorAll('optgroup')].map(g => g.label).join(' | ') }; })()"
  Check "voice menu lists only voices (no add/remove entries)" ($vm.n -gt 0 -and $vm.bad.Count -eq 0 -and $vm.groups -notlike "*Add or remove*") ("$($vm.n) voices; groups: $($vm.groups); other: $($vm.bad -join ', ')")
  $ab = Ev "(() => { const c = document.getElementById('cmdsBtn').getBoundingClientRect(), a = document.getElementById('aboutBtn').getBoundingClientRect(); return { below: a.top >= c.bottom - 1, sameCol: Math.abs(a.left - c.left) < 2, visible: a.width > 0 && a.bottom <= innerHeight, next: document.getElementById('cmdsBtn').nextElementSibling.id }; })()"
  Check "About button right under Commands in the tools bar" ($ab.below -and $ab.sameCol -and $ab.visible -and $ab.next -eq "aboutBtn") ("below=$($ab.below) sameCol=$($ab.sameCol) visible=$($ab.visible)")
  $cc = Ev "document.getElementById('counter').textContent"
  Click (At "document.querySelector('#raDlgBox h3')"); Key "PageDown" "PageDown" 34 $null; Key "ArrowRight" "ArrowRight" 39 $null; Start-Sleep 1
  Check "keys in Commands don't turn the book" ((Ev "document.getElementById('counter').textContent") -eq $cc) $cc
  Shot "1-commands"
  Key "Escape" "Escape" 27 $null; WaitFor "document.getElementById('raDlg').style.display === 'none'" 3 | Out-Null
  Click (At "document.getElementById('aboutBtn')")
  Check "About opens with manual" (WaitFor "document.querySelectorAll('#raDlgBox .ab-sec').length > 15" 5) ("sections=" + (Ev "document.querySelectorAll('#raDlgBox .ab-sec').length"))
  Check "About header version" ((Ev "document.querySelector('#raDlgBox .ab-v').textContent") -like "Version $Version*") (Ev "document.querySelector('#raDlgBox .ab-v').textContent")
  Check "About opens on the Manual tab" ((Ev "document.querySelector('#raDlgBox .ab-tabs .on').textContent") -eq "Manual") ""
  Check "only ONE version history: none in the manual (side Contents, in-text Contents, sections)" ((Ev "(() => { const m = document.querySelector('#raDlgBox [data-pane=man]'); return [...m.querySelectorAll('.ab-nav a, .ab-toc a, .ab-sec h4')].filter(a => /version history/i.test(a.textContent)).length + m.querySelectorAll('.ab-ver').length + (/1\.0\.0\s+\(2026-11-10\)\s+First release/.test(m.textContent) ? 1 : 0); })()") -eq 0) ("side=" + (Ev "document.querySelectorAll('#raDlgBox [data-pane=man] .ab-nav a').length") + " toc=" + (Ev "document.querySelectorAll('#raDlgBox [data-pane=man] .ab-toc a').length"))
  $ni = Ev "[...document.querySelectorAll('#ab-introduction .ab-txt b')].map(b => b.textContent).filter(x => /^New in /.test(x)).join('|')"
  Check "intro has only the latest New in" ($ni -eq "New in ${Version}:") $ni
  Check "no exe hash/size in manual" (-not (Ev "/0FA85429|66,023,496|87B6B789|README\.txt\s+This document/.test(document.getElementById('raDlgBox').textContent)")) ""
  Start-Sleep 1; Shot "2-about-manual"

  $M = "document.querySelector('#raDlgBox [data-pane=man]')"
  $B = "$M.querySelector('.ab-body')"
  $near = { param($tid) "Math.abs($B.scrollTop - ($B.querySelector('#ab-$tid').offsetTop - 6)) <= 3" }

  # every side-list entry: real click, check it lands
  $n = Ev "$M.querySelectorAll('.ab-nav a').length"
  $bad = @()
  for ($k = 0; $k -lt $n; $k++) {
    $tid = Ev "$M.querySelectorAll('.ab-nav a')[$k].dataset.go"
    Click (At "$M.querySelectorAll('.ab-nav a')[$k]")
    if (-not (WaitFor (& $near $tid) 6)) { $bad += $tid }
    elseif ((Ev "($M.querySelector('.ab-nav a.on') || {}).dataset?.go") -ne $tid) { $bad += "$tid(highlight)" }
  }
  Check "side Contents: all $n entries jump + highlight" ($bad.Count -eq 0) ($bad -join ",")

  Click (At "$M.querySelector('.ab-nav a[data-go=s16]')"); WaitFor (& $near "s16") 6 | Out-Null; Start-Sleep 1; Shot "3-troubleshooting"
  # inline "section 3" link inside section 16
  Click (At "$B.querySelector('#ab-s16 a[data-go=s3]')")
  Check "inline 'section 3' link" (WaitFor (& $near "s3") 6) ""

  # Contents section inside the text: every entry
  $bad = @()
  for ($k = 1; $k -le 18; $k++) {
    Ev "$B.style.scrollBehavior='auto'; $B.scrollTop = $B.querySelector('#ab-contents').offsetTop; $B.style.scrollBehavior=''; true" | Out-Null
    Start-Sleep -Milliseconds 150
    Click (At "$B.querySelector('#ab-contents a[data-go=s$k]')")
    if (-not (WaitFor (& $near "s$k") 6)) { $bad += "s$k" }
  }
  Check "in-text Contents: all 18 entries jump" ($bad.Count -eq 0) ($bad -join ",")

  # mouse wheel scroll moves the text and the highlight follows
  Click (At "$M.querySelector('.ab-nav a[data-go=s5]')"); WaitFor (& $near "s5") 6 | Out-Null
  $pt = At "$B.querySelector('#ab-s5 h4')"
  for ($w = 0; $w -lt 12; $w++) { Cdp "Input.dispatchMouseEvent" @{ type = "mouseWheel"; x = $pt.x; y = $pt.y + 200; deltaX = 0; deltaY = 400 } | Out-Null; Start-Sleep -Milliseconds 120 }
  Start-Sleep 1
  $hl = Ev "($M.querySelector('.ab-nav a.on') || {}).dataset?.go"
  Check "wheel scroll + highlight follows" ($hl -ne "s5" -and $hl) "highlight now $hl"

  # keyboard: PageDown / End / Home scroll the text
  $c0 = Ev "document.getElementById('counter').textContent"
  $t1 = Ev "$B.scrollTop"
  Key "PageDown" "PageDown" 34 $null; Start-Sleep 1
  $t2 = Ev "$B.scrollTop"
  Check "PageDown scrolls" ($t2 -gt $t1) "$t1 -> $t2"
  Key "End" "End" 35 $null; Start-Sleep 2
  Check "End reaches the bottom; highlight = 18" ((Ev "($M.querySelector('.ab-nav a.on') || {}).dataset?.go") -eq "s18") ""
  Key "ArrowUp" "ArrowUp" 38 $null; Key "ArrowUp" "ArrowUp" 38 $null; Start-Sleep 1
  Check "arrow keys scroll" ((Ev "$B.scrollTop + $B.clientHeight < $B.scrollHeight - 2")) ""
  Key "Home" "Home" 36 $null; Start-Sleep 2
  Check "Home back to top" ((Ev "$B.scrollTop") -lt 5) ""
  Check "keys did not turn the book page" ((Ev "document.getElementById('counter').textContent") -eq $c0) $c0

  # Version history tab inside About
  Click (At "$M.querySelector('.ab-nav a[data-go=s12]')"); WaitFor (& $near "s12") 6 | Out-Null
  $manTop = Ev "$B.scrollTop"
  Click (At "document.querySelector('#raDlgBox .ab-tabs [data-tab=hist]')")
  $H = "document.querySelector('#raDlgBox [data-pane=hist]')"
  Check "Version history tab" (WaitFor "$H.classList.contains('on')" 3) ("series=" + (Ev "$H.querySelectorAll('.ab-series').length") + " first=" + (Ev "$H.querySelector('.ab-ver .vn').textContent"))
  # v1.8.7: grouped by series; one card per series, earlier versions folded
  $grp = Ev "(() => { const s = [...$H.querySelectorAll('.ab-series')]; return { cards: s.every(x => x.querySelectorAll(':scope > .ab-ver').length === 1), folded: [...$H.querySelectorAll('.ab-fold')].every(f => !f.classList.contains('open')), latest: s[0].querySelector('.ab-ver .vn').textContent === '$Version' }; })()"
  Check "history grouped: one card per series, everything else folded, newest first" ($grp.cards -and $grp.folded -and $grp.latest) ("cards=$($grp.cards) folded=$($grp.folded) latest=$($grp.latest)")
  Start-Sleep 1; Shot "4-history"
  $HB = "$H.querySelector('.ab-body')"
  $bad = @()
  $nv = Ev "$H.querySelectorAll('.ab-nav > a').length"
  for ($k = 0; $k -lt $nv; $k++) {
    $tid = Ev "$H.querySelectorAll('.ab-nav > a')[$k].dataset.go"
    Click (At "$H.querySelectorAll('.ab-nav > a')[$k]")
    $ok = WaitFor "(() => { const b = $HB, e = b.querySelector('#$tid'); return Math.abs(b.scrollTop - (e.offsetTop - 6)) <= 3 || b.scrollTop + b.clientHeight >= b.scrollHeight - 2; })()" 6
    if (-not $ok) { $bad += $tid }
  }
  Check "history: all $nv series jump" ($bad.Count -eq 0) ($bad -join ",")
  # a series in the side list opens its earlier versions there; clicking one opens it in the page and scrolls to it
  Click (At "$H.querySelector('.ab-nav > a[data-go=ser-1-8]')"); Start-Sleep 1
  Check "side list: 1.8 shows its earlier versions" (WaitFor "$H.querySelector('.ab-subnav[data-fold-of=ser-1-8]').classList.contains('open') && $H.querySelector('.ab-subnav[data-fold-of=ser-1-8]').getBoundingClientRect().height > 40" 4) ""
  Click (At "$H.querySelector('.ab-nav a.sub[data-go=v1-8-4]')")
  Check "side list: 1.8.4 opens and is scrolled to" (WaitFor "(() => { const v = $HB.querySelector('#v1-8-4'); return v.querySelector(':scope > .ab-fold').classList.contains('open') && Math.abs($HB.scrollTop - (v.offsetTop - 6)) <= 3 && v.querySelector('.ab-oldbody').getBoundingClientRect().height > 20; })()" 6) ""
  Start-Sleep 1; Shot "4b-history-1.8.4"
  # in the page: "Earlier 1.7 versions" folds out, and a row opens its details
  Click (At "$H.querySelector('.ab-oldtoggle[data-fold=ser-1-7]')")
  Check "side list: only one series expanded at a time" ((Ev "$H.querySelectorAll('.ab-subnav.open').length") -eq 1) ""
  Check "page: 'Earlier 1.7 versions' folds out" (WaitFor "$H.querySelector('#ser-1-7 .ab-older > .ab-fold').classList.contains('open') && $H.querySelector('#ser-1-7 .ab-older > .ab-fold').getBoundingClientRect().height > 100" 4) ""
  Start-Sleep -Milliseconds 600      # let the fold finish sliding open before clicking a row in it
  $pt = At "$H.querySelector('#v1-7-4 .ab-oldhead')"
  $hit = Ev "(() => { const e = document.elementFromPoint($($pt.x), $($pt.y)); return e ? e.tagName + '.' + e.className + ' in ' + ((e.closest('.ab-oldv') || {}).id || '-') : 'nothing'; })()"
  Click $pt
  $opened = WaitFor "$H.querySelector('#v1-7-4 .ab-oldbody').getBoundingClientRect().height > 20" 4
  Check "page: clicking 1.7.4 opens its details" $opened ("open rows: " + (Ev "[...$H.querySelectorAll('.ab-oldhead[aria-expanded=true] .vn')].map(e => e.textContent).join(',')") + "; under the click: $hit at $([int]$pt.x),$([int]$pt.y); fold height " + (Ev "Math.round($H.querySelector('#ser-1-7 .ab-older > .ab-fold').getBoundingClientRect().height)"))
  Start-Sleep -Milliseconds 600
  $pt = At "$H.querySelector('#v1-7-4 .ab-oldhead')"
  $hit = Ev "(() => { const e = document.elementFromPoint($($pt.x), $($pt.y)); return e ? e.tagName + '.' + e.className + ' in ' + ((e.closest('.ab-oldv') || {}).id || '-') : 'nothing'; })()"
  Click $pt
  Start-Sleep -Milliseconds 300
  "  (second click on $hit at $([int]$pt.x),$([int]$pt.y); expanded now: " + (Ev "$H.querySelector('#v1-7-4 .ab-oldhead').getAttribute('aria-expanded')") + "; height " + (Ev "Math.round($H.querySelector('#v1-7-4 > .ab-fold').getBoundingClientRect().height)") + ")"
  Check "page: clicking 1.7.4 again closes it" (WaitFor "$H.querySelector('#v1-7-4 > .ab-fold').getBoundingClientRect().height < 2" 4) ""
  $hc = Ev "$HB.scrollTop"
  Key "PageUp" "PageUp" 33 $null; Start-Sleep 1
  Check "keys scroll the history (not the book)" ((Ev "$HB.scrollTop") -lt $hc -and (Ev "document.getElementById('counter').textContent") -eq $c0) "$hc -> $(Ev "$HB.scrollTop")"
  Click (At "document.querySelector('#raDlgBox .ab-tabs [data-tab=man]')")
  $manTop2 = Ev "$B.scrollTop"
  Check "back to Manual keeps its place" ((Ev "$M.classList.contains('on')") -and [Math]::Abs($manTop2 - $manTop) -le 2) "$manTop -> $manTop2"
  Key "PageUp" "PageUp" 33 $null; Start-Sleep 1
  Check "keys scroll the manual after the tab switch" ((Ev "$B.scrollTop") -lt $manTop2 -and (Ev "document.getElementById('counter').textContent") -eq $c0) ""

  # Esc closes; ? reopens Commands; ✕ closes About
  Key "Escape" "Escape" 27 $null
  Check "Esc closes About" (WaitFor "document.getElementById('raDlg').style.display === 'none'" 3) ""
  Click (At "document.getElementById('aboutBtn')")
  WaitFor "document.querySelectorAll('#raDlgBox .ab-sec').length > 15" 5 | Out-Null
  Click (At "document.querySelector('#raDlgBox .ab-x')")
  Check "close button closes About" (WaitFor "document.getElementById('raDlg').style.display === 'none'" 3) ""

  # other theme (T), reopen with the ? key, screenshot, then T back
  Click (At "document.getElementById('stage') || document.body"); Start-Sleep -Milliseconds 300
  Key "t" "KeyT" 84 "t"; Start-Sleep 1
  $theme1 = Ev "document.documentElement.getAttribute('data-theme') || ''"
  Key "?" "Slash" 191 "?";
  Check "? key opens Commands" (WaitFor "!!document.querySelector('#raDlgBox .cmds')" 3) ""
  Key "Escape" "Escape" 27 $null; Start-Sleep -Milliseconds 300
  Click (At "document.getElementById('aboutBtn')")
  WaitFor "document.querySelectorAll('#raDlgBox .ab-sec').length > 15" 5 | Out-Null
  Start-Sleep 1; Shot "5-about-other-theme"
  Click (At "document.querySelector('#raDlgBox .ab-tabs [data-tab=hist]')"); Start-Sleep 1; Shot "6-history-other-theme"
  Key "Escape" "Escape" 27 $null; Start-Sleep -Milliseconds 300
  Cdp "Emulation.setDeviceMetricsOverride" @{ width = 1280; height = 640; deviceScaleFactor = 1; mobile = $false } | Out-Null; Start-Sleep 1
  $ab2 = Ev "(() => { const a = document.getElementById('aboutBtn').getBoundingClientRect(), t = document.getElementById('tools'); return { bottom: a.bottom, h: innerHeight, scroll: t.scrollHeight > t.clientHeight }; })()"
  Check "About button reachable in a short (640 px) window" ($ab2.bottom -le $ab2.h -or $ab2.scroll) ("bottom=$($ab2.bottom) h=$($ab2.h) toolsScrolls=$($ab2.scroll)")
  Shot "7-short-window"
  Cdp "Emulation.clearDeviceMetricsOverride" @{} | Out-Null
  Check "theme switched for the check" ($theme1 -ne $theme0) "'$theme0' -> '$theme1'"
  Key "Escape" "Escape" 27 $null; Start-Sleep -Milliseconds 300
  Key "t" "KeyT" 84 "t"; Start-Sleep 1
  Check "theme restored" ((Ev "document.documentElement.getAttribute('data-theme') || ''") -eq $theme0) ""

  # v1.8.8 page links: the switch (L), Backspace with nothing to go back to, and (with -LinkPage) real clicks
  $cnt = { Ev "document.getElementById('counter').textContent" }
  Check "Page links tool is on by default" (Ev "document.getElementById('linkBtn').classList.contains('on')") ""
  Click (At "document.getElementById('cmdsBtn')")
  WaitFor "!!document.querySelector('#raDlgBox .cmds')" 3 | Out-Null
  Check "Commands lists page links and their keys" (Ev "(() => { const t = document.getElementById('raDlgBox').textContent; return /contents line/.test(t) && /Backspace/.test(t) && /Page links on/.test(t); })()") ""
  Key "Escape" "Escape" 27 $null; Start-Sleep -Milliseconds 300
  Click (At "document.getElementById('stage')"); Start-Sleep 1
  $c1 = & $cnt
  Key "l" "KeyL" 76 "l"; Start-Sleep -Milliseconds 400
  $off = Ev "!document.getElementById('linkBtn').classList.contains('on') && localStorage.getItem('flip-links') === '0'"
  Key "l" "KeyL" 76 "l"; Start-Sleep -Milliseconds 400
  Check "L switches page links off and on (remembered)" ($off -and (Ev "document.getElementById('linkBtn').classList.contains('on') && localStorage.getItem('flip-links') === '1'")) ""
  Key "Backspace" "Backspace" 8 $null; Start-Sleep 1
  Check "Backspace with no link followed does nothing" ((& $cnt) -eq $c1 -and (Ev "document.getElementById('linkBack').style.display === 'none'")) (& $cnt)
  if ($LinkPage -gt 0) {
    $lp = $LinkPage
    Ev "(() => { const i = document.getElementById('gotoInput'); i.value = '$lp'; i.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' })); return true; })()" | Out-Null
    $hm = Ev "new Promise(r => setTimeout(() => r(document.getElementById('counter').textContent), 2500))"
    $found = WaitFor "(window.flipbookLinks($lp) || []).length >= 3" 40
    Check "contents page $lp has links" $found ("links=" + (Ev "(window.flipbookLinks($lp) || []).length") + "; $hm")
    if ($found) {
      $pg = { param($k) Ev "(() => { const L = flipbookLinks($lp)[$k], el = [document.getElementById('left'), document.getElementById('right')].find(e => +e.dataset.page === $lp), r = el.getBoundingClientRect(); return { x: r.left + (L.box[0] + L.box[2]) / 2 * r.width, y: r.top + (L.box[1] + L.box[3]) / 2 * r.height, to: L.to }; })()" }
      $state = { Ev "(() => { const b = document.getElementById('book').getBoundingClientRect(), R = flipbookReader(); return 'book=' + [b.left, b.top, b.width, b.height].map(Math.round).join(',') + ' pos=' + R.pos + ' anim=' + R.animating + ' links=' + (flipbookLinks($lp) || []).length + ' pt=' + flipbookPageText($lp) + ' shown=' + document.getElementById('left').dataset.page + '/' + document.getElementById('right').dataset.page + ' title=' + document.getElementById('book').title; })()" }
      $e = & $pg 3
      $want = Ev "flipbookFindPrinted('$($e.to.Substring(3))', $lp)"
      $s0 = & $state
      $e = & $pg 3            # measured again after the look-up, right before the pointer moves
      MouseTo $e; Start-Sleep -Milliseconds 400
      Check "hover over a contents line: hand cursor + mark + tooltip" (Ev "(() => { const b = document.getElementById('book'); return b.classList.contains('onLink') && getComputedStyle(b).cursor === 'pointer' && !!document.querySelector('.linkhi') && /Go to page/.test(b.title); })()") ("before: $s0 | after: " + (& $state) + " | at " + [int]$e.x + "," + [int]$e.y)
      Shot "8-link-hover"
      Click $e
      $ok = WaitFor "(() => { const c = document.getElementById('counter').textContent.match(/\d+/g).map(Number); return c.slice(0, -1).includes($want); })()" 15
      Check "click on '$($e.to)' goes to file page $want" $ok ((& $cnt) + "; back: " + (Ev "document.getElementById('linkBack').textContent"))
      Check "'Back to page $lp' shows in the bottom bar" (Ev "(() => { const b = document.getElementById('linkBack'); return b.style.display !== 'none' && b.textContent.includes('$lp') && b.getBoundingClientRect().width > 0; })()") ""
      Start-Sleep 1; Shot "9-after-link"
      Key "Backspace" "Backspace" 8 $null
      Check "Backspace goes back to the contents page" (WaitFor "document.getElementById('counter').textContent === '$hm'" 8) (& $cnt)
      Start-Sleep 1
      $e = & $pg 4
      Click $e; WaitFor "document.getElementById('counter').textContent !== '$hm' && document.getElementById('linkBack').style.display !== 'none'" 15 | Out-Null; Start-Sleep 1
      Key "ArrowLeft" "ArrowLeft" 37 $null 1
      Check "Alt+Left goes back too" (WaitFor "document.getElementById('counter').textContent === '$hm'" 8) (& $cnt)
      Start-Sleep 1
      # a double-click on a link only jumps (no close-up)
      $e = & $pg 2
      Cdp "Input.dispatchMouseEvent" @{ type = "mousePressed"; x = $e.x; y = $e.y; button = "left"; clickCount = 1 } | Out-Null
      Cdp "Input.dispatchMouseEvent" @{ type = "mouseReleased"; x = $e.x; y = $e.y; button = "left"; clickCount = 1 } | Out-Null
      Cdp "Input.dispatchMouseEvent" @{ type = "mousePressed"; x = $e.x; y = $e.y; button = "left"; clickCount = 2 } | Out-Null
      Cdp "Input.dispatchMouseEvent" @{ type = "mouseReleased"; x = $e.x; y = $e.y; button = "left"; clickCount = 2 } | Out-Null
      Start-Sleep 2
      Check "double-click on a link jumps without the close-up" ((& $cnt) -ne $hm -and (Ev "document.getElementById('zoom').style.display !== 'block'")) (& $cnt)
      Click (At "document.getElementById('linkBack')")
      WaitFor "document.getElementById('counter').textContent === '$hm'" 8 | Out-Null; Start-Sleep 1
      # a click away from the lines (bottom margin of the page) still turns the page
      $m = Ev "(() => { const el = [document.getElementById('left'), document.getElementById('right')].find(e => +e.dataset.page === $lp), r = el.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.bottom - 12 }; })()"
      Click $m
      Check "a click off the links still turns the page" (WaitFor "document.getElementById('counter').textContent !== '$hm'" 6) (& $cnt)
      Start-Sleep 1
      # a drag that starts on a link turns the page (no jump)
      Ev "(() => { const i = document.getElementById('gotoInput'); i.value = '$lp'; i.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' })); return true; })()" | Out-Null
      WaitFor "document.getElementById('counter').textContent === '$hm' && (window.flipbookLinks($lp) || []).length >= 3" 10 | Out-Null; Start-Sleep 1
      $e = & $pg 3
      $right = Ev "+document.getElementById('right').dataset.page === $lp"
      $dx = if ($right) { -1 } else { 1 }
      Cdp "Input.dispatchMouseEvent" @{ type = "mousePressed"; x = $e.x; y = $e.y; button = "left"; clickCount = 1 } | Out-Null
      for ($s = 1; $s -le 12; $s++) { Cdp "Input.dispatchMouseEvent" @{ type = "mouseMoved"; x = $e.x + $dx * $s * 40; y = $e.y; button = "left" } | Out-Null; Start-Sleep -Milliseconds 30 }
      Cdp "Input.dispatchMouseEvent" @{ type = "mouseReleased"; x = $e.x + $dx * 480; y = $e.y; button = "left"; clickCount = 1 } | Out-Null
      Start-Sleep 2
      $after = & $cnt
      Check "a drag that starts on a link turns one page (no jump)" ($after -ne $hm -and (Ev "document.getElementById('linkBack').style.display === 'none'")) $after
    }
  }

  # v1.8.6 security: the Content Security Policy is active in the real window, and the pywebview bridge still works under it
  Check "CSP blocks text-to-code (eval / new Function)" (Ev "(() => { try { new Function('return 1')(); return false; } catch (e) { return e instanceof EvalError; } })()") ""
  Check "CSP blocks connections to the internet" (Ev "fetch('https://example.com/', { mode: 'no-cors' }).then(() => false, () => true)") ""
  $fs = Ev "(async () => { const a = window.pywebview && window.pywebview.api; if (!a) return 'no bridge'; await a.toggle_fullscreen(); await new Promise(r => setTimeout(r, 800)); await a.toggle_fullscreen(); return 'ok'; })()"
  Check "pywebview bridge works under the CSP (full screen on/off)" ($fs -eq "ok") $fs

  $errs = Ev "(window.__errs || []).length"
} catch { [void]$results.Add("FAIL  script error  $($_.Exception.Message)") }
finally {
  $results
  $ids = @((Get-Process Flipbook -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Id }).Id)
  (Get-Process -Id $ids -ErrorAction SilentlyContinue | Where-Object MainWindowTitle).CloseMainWindow() | Out-Null
  Start-Sleep 4
  Get-Process -Id $ids -ErrorAction SilentlyContinue | Stop-Process -Force
  "test processes left: " + @(Get-Process -Id $ids -ErrorAction SilentlyContinue).Count
}
