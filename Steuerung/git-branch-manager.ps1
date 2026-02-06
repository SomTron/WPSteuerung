# Git Branch Manager - Interaktives Skript fuer Branch-Verwaltung
# Verwendung: .\git-branch-manager.ps1

param(
    [switch]$Pull,
    [switch]$Push
)

function Show-Menu {
    param (
        [string]$Title,
        [array]$Options
    )
    
    Write-Host "`n=== $Title ===" -ForegroundColor Cyan
    for ($i = 0; $i -lt $Options.Length; $i++) {
        Write-Host "$($i + 1). $($Options[$i])"
    }
    Write-Host "0. Abbrechen" -ForegroundColor Red
    
    do {
        $selection = Read-Host "`nWaehle eine Option (0-$($Options.Length))"
        $number = $selection -as [int]
    } while ($number -eq $null -or $number -lt 0 -or $number -gt $Options.Length)
    
    if ($number -eq 0) {
        Write-Host "Abgebrochen." -ForegroundColor Yellow
        return $null
    }
    
    return $Options[$number - 1]
}

function Get-LocalBranches {
    $branches = git branch --format="%(refname:short)"
    return $branches
}

function Get-RemoteBranches {
    $branches = git branch -r --format="%(refname:short)" | Where-Object { $_ -notmatch "HEAD" }
    return $branches | ForEach-Object { $_ -replace "^origin/", "" }
}

function Pull-Branch {
    Write-Host "`n--- Branch von GitHub aktualisieren ---" -ForegroundColor Green
    
    # Aktuellen Branch anzeigen
    $currentBranch = git branch --show-current
    Write-Host "Aktueller Branch: $currentBranch" -ForegroundColor Yellow
    
    # Fetch all
    Write-Host "`nHole neueste Informationen von GitHub..." -ForegroundColor Cyan
    git fetch --all
    
    # Branch auswaehlen
    $branches = Get-LocalBranches
    $selectedBranch = Show-Menu -Title "Welchen Branch moechtest du aktualisieren?" -Options $branches
    
    if ($selectedBranch -eq $null) { return }
    
    # Wechsle zum Branch
    if ($selectedBranch -ne $currentBranch) {
        Write-Host "`nWechsle zu Branch '$selectedBranch'..." -ForegroundColor Cyan
        git checkout $selectedBranch
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Fehler beim Wechseln des Branches!" -ForegroundColor Red
            return
        }
    }
    
    # Pull mit Rebase
    Write-Host "`nAktualisiere Branch '$selectedBranch' von GitHub..." -ForegroundColor Cyan
    
    # Erst versuchen mit rebase
    $pullOutput = git pull origin $selectedBranch --rebase 2>&1 | Out-String
    
    if ($LASTEXITCODE -eq 0) {
        Write-Host "`nBranch '$selectedBranch' erfolgreich aktualisiert!" -ForegroundColor Green
    }
    else {
        Write-Host "`nRebase fehlgeschlagen oder abgebrochen." -ForegroundColor Yellow
        Write-Host $pullOutput
        
        # Rebase abbrechen falls haengengeblieben
        git rebase --abort 2>$null
        
        # Alternative anbieten
        Write-Host "`nMoechtest du stattdessen ein normales Merge versuchen? (j/n)" -ForegroundColor Cyan
        $tryMerge = Read-Host
        
        if ($tryMerge -eq "j" -or $tryMerge -eq "J") {
            Write-Host "`nVersuche normales Pull (Merge)..." -ForegroundColor Cyan
            git pull origin $selectedBranch
            
            if ($LASTEXITCODE -eq 0) {
                Write-Host "`nBranch '$selectedBranch' erfolgreich aktualisiert!" -ForegroundColor Green
            }
            else {
                Write-Host "`nFehler beim Aktualisieren!" -ForegroundColor Red
            }
        }
        else {
            Write-Host "`nAbgebrochen. Branch wurde NICHT aktualisiert." -ForegroundColor Red
        }
    }
}

function Push-Branch {
    Write-Host "`n--- Aenderungen zu GitHub pushen ---" -ForegroundColor Green
    
    # Status mit Farben anzeigen
    Write-Host "`nGit Status:" -ForegroundColor Cyan
    
    # Pruefen ob es Aenderungen gibt
    $status = git status --porcelain
    if ([string]::IsNullOrWhiteSpace($status)) {
        Write-Host "`nKeine Aenderungen zum Committen." -ForegroundColor Yellow
        return
    }
    
    # Aenderungen kategorisieren
    $modified = @()
    $added = @()
    $deleted = @()
    $untracked = @()
    
    $status -split "`n" | ForEach-Object {
        $line = $_.Trim()
        if ($line) {
            $statusCode = $line.Substring(0, 2)
            $file = $line.Substring(3)
            
            if ($statusCode -match "^.D" -or $statusCode -match "^D.") {
                $deleted += $file
            }
            elseif ($statusCode -match "^.M" -or $statusCode -match "^M.") {
                $modified += $file
            }
            elseif ($statusCode -match "^A.") {
                $added += $file
            }
            elseif ($statusCode -match "^\?\?") {
                $untracked += $file
            }
        }
    }
    
    # Aenderungen anzeigen
    if ($modified.Count -gt 0) {
        Write-Host "`nModifiziert ($($modified.Count)):" -ForegroundColor Yellow
        $modified | ForEach-Object { Write-Host "  M $_" -ForegroundColor Yellow }
    }
    
    if ($added.Count -gt 0) {
        Write-Host "`nNeu ($($added.Count)):" -ForegroundColor Green
        $added | ForEach-Object { Write-Host "  + $_" -ForegroundColor Green }
    }
    
    if ($untracked.Count -gt 0) {
        Write-Host "`nUntracked ($($untracked.Count)):" -ForegroundColor Cyan
        $untracked | ForEach-Object { Write-Host "  ? $_" -ForegroundColor Cyan }
    }
    
    # KRITISCH: Geloeschte Dateien hervorheben
    if ($deleted.Count -gt 0) {
        Write-Host "`n!!! ACHTUNG: GELOESCHTE DATEIEN ($($deleted.Count)) !!!" -ForegroundColor Red -BackgroundColor Black
        $deleted | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
        
        Write-Host "`nMoechtest du diese Loeschungen committen?" -ForegroundColor Yellow
        Write-Host "1. Ja, alles committen (inkl. Loeschungen)"
        Write-Host "2. Nein, Loeschungen nicht committen"
        Write-Host "0. Abbrechen"
        
        $choice = Read-Host "`nWaehle (0-2)"
        
        if ($choice -eq "0") {
            Write-Host "Abgebrochen." -ForegroundColor Yellow
            return
        }
        elseif ($choice -eq "2") {
            Write-Host "`nLoeschungen werden NICHT committed." -ForegroundColor Green
            # Geloeschte Dateien aus Staging entfernen falls hinzugefuegt
            $deleted | ForEach-Object {
                git reset HEAD $_ 2>$null
            }
        }
    }
    
    # Commit Message
    Write-Host ""
    $commitMsg = Read-Host "Commit-Nachricht eingeben"
    if ([string]::IsNullOrWhiteSpace($commitMsg)) {
        Write-Host "Abgebrochen - keine Commit-Nachricht." -ForegroundColor Red
        return
    }
    
    # Escape quotes in commit message
    $commitMsg = $commitMsg -replace '"', '`"'
    
    # Add changes
    Write-Host "`nFuege Aenderungen hinzu..." -ForegroundColor Cyan
    git add . 2>&1 | Out-Null
    
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Fehler beim Hinzufuegen der Aenderungen!" -ForegroundColor Red
        return
    }
    
    # Commit with properly quoted message
    Write-Host "Erstelle Commit..." -ForegroundColor Cyan
    try {
        $commitOutput = git commit -m "$commitMsg" 2>&1 | Out-String
        
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Fehler beim Committen!" -ForegroundColor Red
            Write-Host $commitOutput
            return
        }
        
        Write-Host "Commit erfolgreich erstellt!" -ForegroundColor Green
    }
    catch {
        Write-Host "Fehler beim Committen: $_" -ForegroundColor Red
        return
    }
    
    # Branch zum Pushen auswaehlen
    try {
        $currentBranch = git branch --show-current 2>&1
        if ([string]::IsNullOrWhiteSpace($currentBranch)) {
            Write-Host "Fehler: Konnte aktuellen Branch nicht ermitteln!" -ForegroundColor Red
            return
        }
        Write-Host "`nAktueller Branch: $currentBranch" -ForegroundColor Yellow
    }
    catch {
        Write-Host "Fehler beim Ermitteln des aktuellen Branches: $_" -ForegroundColor Red
        return
    }
    
    try {
        $branches = Get-LocalBranches
        if ($branches.Count -eq 0) {
            Write-Host "Fehler: Keine Branches gefunden!" -ForegroundColor Red
            return
        }
    }
    catch {
        Write-Host "Fehler beim Abrufen der Branches: $_" -ForegroundColor Red
        return
    }
    
    Write-Host "`nZu welchem/welchen Branch(es) moechtest du pushen?" -ForegroundColor Cyan
    Write-Host "1. Gleicher Branch ($currentBranch)" -ForegroundColor Green
    Write-Host "2. Einen anderen Branch waehlen"
    Write-Host "3. Mehrere Branches waehlen (z.B. master + android-api)" -ForegroundColor Yellow
    Write-Host "0. Abbrechen" -ForegroundColor Red
    
    $choice = Read-Host "`nWaehle (0-3)"
    
    $targetBranches = @()
    
    try {
        if ($choice -eq "1") {
            $targetBranches = @($currentBranch)
        }
        elseif ($choice -eq "2") {
            $targetBranch = Show-Menu -Title "Ziel-Branch waehlen" -Options $branches
            if ($targetBranch -eq $null) { 
                Write-Host "Abgebrochen." -ForegroundColor Yellow
                return 
            }
            
            # Wenn anderer Branch, Warnung
            if ($targetBranch -ne $currentBranch) {
                Write-Host "`nWARNUNG: Du pushst zu einem anderen Branch!" -ForegroundColor Yellow
                $confirm = Read-Host "Moechtest du zu '$targetBranch' pushen? (j/n)"
                if ($confirm -ne "j" -and $confirm -ne "J") {
                    Write-Host "Abgebrochen." -ForegroundColor Red
                    return
                }
            }
            $targetBranches = @($targetBranch)
        }
        elseif ($choice -eq "3") {
            # Multi-select
            Write-Host "`nWaehle Branches (mit Komma getrennt, z.B. 1,2,3):" -ForegroundColor Cyan
            for ($i = 0; $i -lt $branches.Length; $i++) {
                Write-Host "$($i + 1). $($branches[$i])"
            }
            
            $selections = Read-Host "`nNummern eingeben (z.B. 1,3)"
            if ([string]::IsNullOrWhiteSpace($selections)) {
                Write-Host "Abgebrochen - keine Auswahl." -ForegroundColor Yellow
                return
            }
            
            $numbers = $selections -split "," | ForEach-Object { $_.Trim() }
            
            foreach ($num in $numbers) {
                try {
                    $index = [int]$num - 1
                    if ($index -ge 0 -and $index -lt $branches.Length) {
                        $targetBranches += $branches[$index]
                    }
                }
                catch {
                    Write-Host "Warnung: Ungueltige Nummer '$num' uebersprungen." -ForegroundColor Yellow
                }
            }
            
            if ($targetBranches.Count -eq 0) {
                Write-Host "Keine gueltigen Branches gewaehlt!" -ForegroundColor Red
                return
            }
            
            Write-Host "`nPushe zu folgenden Branches:" -ForegroundColor Yellow
            $targetBranches | ForEach-Object { Write-Host "  - $_" }
            
            $confirm = Read-Host "`nFortfahren? (j/n)"
            if ($confirm -ne "j" -and $confirm -ne "J") {
                Write-Host "Abgebrochen." -ForegroundColor Red
                return
            }
        }
        elseif ($choice -eq "0") {
            Write-Host "Abgebrochen." -ForegroundColor Yellow
            return
        }
        else {
            Write-Host "Ungueltige Auswahl!" -ForegroundColor Red
            return
        }
    }
    catch {
        Write-Host "Fehler bei der Branch-Auswahl: $_" -ForegroundColor Red
        Write-Host "Stack Trace: $($_.ScriptStackTrace)" -ForegroundColor Gray
        return
    }
    
    # Push zu allen ausgewaehlten Branches
    $successCount = 0
    $failCount = 0
    
    foreach ($branch in $targetBranches) {
        try {
            Write-Host "`nPushe zu GitHub (origin/$branch)..." -ForegroundColor Cyan
            
            # Capture output to detect errors
            $pushOutput = git push origin HEAD:$branch 2>&1 | Out-String
            
            if ($LASTEXITCODE -eq 0) {
                Write-Host "Erfolgreich zu '$branch' gepusht!" -ForegroundColor Green
                $successCount++
            }
            else {
                Write-Host "Push fehlgeschlagen!" -ForegroundColor Red
                Write-Host $pushOutput -ForegroundColor Gray
                
                # Check for non-fast-forward
                if ($pushOutput -match "non-fast-forward" -or $pushOutput -match "fetch first" -or $pushOutput -match "rejected") {
                    Write-Host "`nDer Remote-Branch '$branch' hat Aenderungen, die du nicht hast." -ForegroundColor Yellow
                    
                    if ($branch -ne $currentBranch) {
                        try {
                            $autoMerge = Read-Host "Moechtest du automatisch mergen? (Checkout $branch -> Merge $currentBranch -> Push -> Checkout back) (j/n)"
                            if ($autoMerge -eq "j" -or $autoMerge -eq "J") {
                                Write-Host "Versuche automatischen Merge..." -ForegroundColor Cyan
                                
                                # 1. Checkout target branch
                                Write-Host "1. Wechsle zu $branch..." -ForegroundColor Cyan
                                git checkout $branch 2>&1 | Out-Null
                                if ($LASTEXITCODE -ne 0) { 
                                    Write-Host "Konnte nicht zu $branch wechseln. Versuche ihn lokal zu erstellen..." -ForegroundColor Yellow
                                    git checkout -b $branch origin/$branch 2>&1 | Out-Null
                                    if ($LASTEXITCODE -ne 0) { 
                                        Write-Host "Fehler beim Checkout." -ForegroundColor Red
                                        $failCount++
                                        continue 
                                    }
                                }
                                
                                # 2. Pull remote changes
                                Write-Host "2. Hole Remote-Aenderungen..." -ForegroundColor Cyan
                                git pull origin $branch 2>&1 | Out-Null
                                
                                # 3. Merge original branch
                                Write-Host "3. Merge $currentBranch..." -ForegroundColor Cyan
                                $mergeOutput = git merge $currentBranch 2>&1 | Out-String
                                if ($LASTEXITCODE -ne 0) {
                                    Write-Host "MERGE KONFLIKT! Bitte manuell loesen." -ForegroundColor Red
                                    Write-Host $mergeOutput -ForegroundColor Gray
                                    git merge --abort 2>&1 | Out-Null
                                    git checkout $currentBranch 2>&1 | Out-Null
                                    $failCount++
                                    continue
                                }
                                
                                # 4. Push
                                Write-Host "4. Pushe..." -ForegroundColor Cyan
                                git push origin $branch 2>&1 | Out-Null
                                if ($LASTEXITCODE -eq 0) {
                                    Write-Host "Erfolgreich gemerged und gepusht!" -ForegroundColor Green
                                    $successCount++
                                }
                                else {
                                    Write-Host "Push nach Merge fehlgeschlagen." -ForegroundColor Red
                                    $failCount++
                                }
                                
                                # 5. Switch back
                                Write-Host "5. Wechsle zurueck zu $currentBranch..." -ForegroundColor Cyan
                                git checkout $currentBranch 2>&1 | Out-Null
                                continue
                            }
                            else {
                                Write-Host "Ueberspringe $branch." -ForegroundColor Yellow
                                $failCount++
                            }
                        }
                        catch {
                            Write-Host "Fehler beim Auto-Merge: $_" -ForegroundColor Red
                            # Sicherstellen dass wir zum Original-Branch zurueckkehren
                            git checkout $currentBranch 2>&1 | Out-Null
                            $failCount++
                        }
                    }
                    else {
                        # Same branch push failed
                        try {
                            $doPull = Read-Host "Moechtest du 'git pull --rebase' ausfuehren und nochmal pushen? (j/n)"
                            if ($doPull -eq "j" -or $doPull -eq "J") {
                                Write-Host "Fuehre Pull mit Rebase aus..." -ForegroundColor Cyan
                                git pull origin $branch --rebase 2>&1 | Out-Null
                                if ($LASTEXITCODE -eq 0) {
                                    Write-Host "Versuche erneut zu pushen..." -ForegroundColor Cyan
                                    git push origin HEAD:$branch 2>&1 | Out-Null
                                    if ($LASTEXITCODE -eq 0) {
                                        Write-Host "Erfolgreich gepusht!" -ForegroundColor Green
                                        $successCount++
                                        continue
                                    }
                                    else {
                                        Write-Host "Push immer noch fehlgeschlagen." -ForegroundColor Red
                                        $failCount++
                                    }
                                }
                                else {
                                    Write-Host "Pull mit Rebase fehlgeschlagen." -ForegroundColor Red
                                    $failCount++
                                }
                            }
                            else {
                                $failCount++
                            }
                        }
                        catch {
                            Write-Host "Fehler beim Pull/Rebase: $_" -ForegroundColor Red
                            $failCount++
                        }
                    }
                }
                else {
                    # Anderer Fehler
                    $failCount++
                }
            }
        }
        catch {
            Write-Host "Unerwarteter Fehler beim Pushen zu '$branch': $_" -ForegroundColor Red
            Write-Host "Stack Trace: $($_.ScriptStackTrace)" -ForegroundColor Gray
            $failCount++
            # Sicherstellen dass wir zum Original-Branch zurueckkehren
            try {
                $currentCheck = git branch --show-current 2>&1
                if ($currentCheck -ne $currentBranch) {
                    Write-Host "Kehre zu $currentBranch zurueck..." -ForegroundColor Yellow
                    git checkout $currentBranch 2>&1 | Out-Null
                }
            }
            catch {
                Write-Host "Warnung: Konnte nicht zu $currentBranch zurueckkehren." -ForegroundColor Red
            }
        }
    }
    
    # Zusammenfassung
    Write-Host "`n=== Zusammenfassung ===" -ForegroundColor Cyan
    Write-Host "Erfolgreich: $successCount" -ForegroundColor Green
    if ($failCount -gt 0) {
        Write-Host "Fehlgeschlagen: $failCount" -ForegroundColor Red
    }
    Write-Host "" # Leerzeile am Ende

}

function Show-MainMenu {
    Write-Host "`n===============================================" -ForegroundColor Cyan
    Write-Host "   Git Branch Manager" -ForegroundColor Cyan
    Write-Host "===============================================" -ForegroundColor Cyan
    
    $currentBranch = git branch --show-current
    Write-Host "`nAktueller Branch: " -NoNewline
    Write-Host "$currentBranch" -ForegroundColor Yellow
    
    Write-Host "`nWas moechtest du tun?" -ForegroundColor Cyan
    Write-Host "1. Branch von GitHub aktualisieren (Pull)" -ForegroundColor Green
    Write-Host "2. Aenderungen zu GitHub pushen (Push)" -ForegroundColor Green
    Write-Host "3. Beides (Pull dann Push)" -ForegroundColor Green
    Write-Host "0. Beenden" -ForegroundColor Red
    
    $choice = Read-Host "`nWaehle (0-3)"
    
    switch ($choice) {
        "1" { Pull-Branch }
        "2" { Push-Branch }
        "3" { 
            Pull-Branch
            Write-Host "`n" -NoNewline
            Read-Host "Druecke Enter um fortzufahren mit Push"
            Push-Branch
        }
        "0" { 
            Write-Host "`nAuf Wiedersehen!" -ForegroundColor Cyan
            return 
        }
        default { 
            Write-Host "`nUngueltige Auswahl!" -ForegroundColor Red
            Show-MainMenu
        }
    }
}

# Hauptprogramm
try {
    # Pruefe ob wir in einem Git-Repository sind
    $gitRoot = git rev-parse --show-toplevel 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Fehler: Kein Git-Repository gefunden!" -ForegroundColor Red
        exit 1
    }
    
    # Wenn Parameter uebergeben wurden
    if ($Pull) {
        Pull-Branch
    }
    elseif ($Push) {
        Push-Branch
    }
    else {
        # Interaktiver Modus
        Show-MainMenu
    }
    
}
catch {
    Write-Host "`nFehler: $_" -ForegroundColor Red
    exit 1
}
