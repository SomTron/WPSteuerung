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
    
    # Add changes
    Write-Host "`nFuege Aenderungen hinzu..." -ForegroundColor Cyan
    git add .
    
    # Commit
    Write-Host "Erstelle Commit..." -ForegroundColor Cyan
    git commit -m $commitMsg
    
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Fehler beim Committen!" -ForegroundColor Red
        return
    }
    
    # Branch zum Pushen auswaehlen
    $currentBranch = git branch --show-current
    Write-Host "`nAktueller Branch: $currentBranch" -ForegroundColor Yellow
    
    $branches = Get-LocalBranches
    Write-Host "`nZu welchem/welchen Branch(es) moechtest du pushen?" -ForegroundColor Cyan
    Write-Host "1. Gleicher Branch ($currentBranch)" -ForegroundColor Green
    Write-Host "2. Einen anderen Branch waehlen"
    Write-Host "3. Mehrere Branches waehlen (z.B. master + android-api)" -ForegroundColor Yellow
    Write-Host "0. Abbrechen" -ForegroundColor Red
    
    $choice = Read-Host "`nWaehle (0-3)"
    
    $targetBranches = @()
    
    if ($choice -eq "1") {
        $targetBranches = @($currentBranch)
    }
    elseif ($choice -eq "2") {
        $targetBranch = Show-Menu -Title "Ziel-Branch waehlen" -Options $branches
        if ($targetBranch -eq $null) { return }
        
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
        $numbers = $selections -split "," | ForEach-Object { $_.Trim() }
        
        foreach ($num in $numbers) {
            $index = [int]$num - 1
            if ($index -ge 0 -and $index -lt $branches.Length) {
                $targetBranches += $branches[$index]
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
    
    # Push zu allen ausgewaehlten Branches
    $successCount = 0
    $failCount = 0
    
    foreach ($branch in $targetBranches) {
        Write-Host "`nPushe zu GitHub (origin/$branch)..." -ForegroundColor Cyan
        
        # Capture output to detect errors
        $pushOutput = git push origin HEAD:$branch 2>&1 | Out-String
        Write-Host $pushOutput
        
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Erfolgreich zu '$branch' gepusht!" -ForegroundColor Green
            $successCount++
        }
        else {
            Write-Host "Fehler beim Pushen zu '$branch'!" -ForegroundColor Red
            
            # Check for non-fast-forward
            if ($pushOutput -match "non-fast-forward" -or $pushOutput -match "fetch first" -or $pushOutput -match "rejected") {
                Write-Host "`nDer Remote-Branch '$branch' hat Aenderungen, die du nicht hast." -ForegroundColor Yellow
                
                if ($branch -ne $currentBranch) {
                    $autoMerge = Read-Host "Moechtest du automatisch mergen? (Checkout $branch -> Merge $currentBranch -> Push -> Checkout back) (j/n)"
                    if ($autoMerge -eq "j") {
                        Write-Host "Versuche automatischen Merge..." -ForegroundColor Cyan
                         
                        # 1. Checkout target branch
                        git checkout $branch
                        if ($LASTEXITCODE -ne 0) { 
                            Write-Host "Konnte nicht zu $branch wechseln. Versuche ihn lokal zu erstellen..."
                            git checkout -b $branch origin/$branch
                            if ($LASTEXITCODE -ne 0) { Write-Host "Fehler beim Checkout."; $failCount++; continue }
                        }
                         
                        # 2. Pull remote changes
                        git pull origin $branch
                         
                        # 3. Merge original branch
                        git merge $currentBranch
                        if ($LASTEXITCODE -ne 0) {
                            Write-Host "MERGE KONFLIKT! Bitte manuell loesen." -ForegroundColor Red
                            git merge --abort
                            git checkout $currentBranch
                            $failCount++
                            continue
                        }
                         
                        # 4. Push
                        git push origin $branch
                        if ($LASTEXITCODE -eq 0) {
                            Write-Host "Erfolgreich gemerged und gepusht!" -ForegroundColor Green
                            $successCount++
                        }
                        else {
                            Write-Host "Push nach Merge immer noch fehlgeschlagen." -ForegroundColor Red
                            $failCount++
                        }
                         
                        # 5. Switch back
                        git checkout $currentBranch
                        continue
                    }
                }
                else {
                    # Same branch push failed
                    $doPull = Read-Host "Moechtest du 'git pull --rebase' ausfuehren und nochmal pushen? (j/n)"
                    if ($doPull -eq "j") {
                        git pull origin $branch --rebase
                        if ($LASTEXITCODE -eq 0) {
                            git push origin HEAD:$branch
                            if ($LASTEXITCODE -eq 0) {
                                Write-Host "Erfolgreich gepusht!" -ForegroundColor Green
                                $successCount++
                                continue
                            }
                        }
                    }
                }
            }
            $failCount++
        }
    }
    
    # Zusammenfassung
    Write-Host "`n=== Zusammenfassung ===" -ForegroundColor Cyan
    Write-Host "Erfolgreich: $successCount" -ForegroundColor Green
    if ($failCount -gt 0) {
        Write-Host "Fehlgeschlagen: $failCount" -ForegroundColor Red
    }

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
