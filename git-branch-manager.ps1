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
    git pull origin $selectedBranch --rebase
    
    if ($LASTEXITCODE -eq 0) {
        Write-Host "`nBranch '$selectedBranch' erfolgreich aktualisiert!" -ForegroundColor Green
    } else {
        Write-Host "`nFehler beim Aktualisieren!" -ForegroundColor Red
    }
}

function Push-Branch {
    Write-Host "`n--- Aenderungen zu GitHub pushen ---" -ForegroundColor Green
    
    # Status anzeigen
    Write-Host "`nGit Status:" -ForegroundColor Cyan
    git status --short
    
    # Pruefen ob es Aenderungen gibt
    $status = git status --porcelain
    if ([string]::IsNullOrWhiteSpace($status)) {
        Write-Host "`nKeine Aenderungen zum Committen." -ForegroundColor Yellow
        return
    }
    
    # Commit Message
    $commitMsg = Read-Host "`nCommit-Nachricht eingeben"
    if ([string]::IsNullOrWhiteSpace($commitMsg)) {
        Write-Host "Abgebrochen - keine Commit-Nachricht." -ForegroundColor Red
        return
    }
    
    # Add all changes
    Write-Host "`nFuege alle Aenderungen hinzu..." -ForegroundColor Cyan
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
    Write-Host "`nZu welchem Branch moechtest du pushen?" -ForegroundColor Cyan
    Write-Host "1. Gleicher Branch ($currentBranch)" -ForegroundColor Green
    Write-Host "2. Anderen Branch waehlen"
    Write-Host "0. Abbrechen" -ForegroundColor Red
    
    $choice = Read-Host "`nWaehle (0-2)"
    
    $targetBranch = $currentBranch
    
    if ($choice -eq "2") {
        $targetBranch = Show-Menu -Title "Ziel-Branch waehlen" -Options $branches
        if ($targetBranch -eq $null) { return }
        
        # Wenn anderer Branch, erst mergen/cherry-pick?
        if ($targetBranch -ne $currentBranch) {
            Write-Host "`nWARNUNG: Du pushst zu einem anderen Branch!" -ForegroundColor Yellow
            $confirm = Read-Host "Moechtest du zu '$targetBranch' pushen? (j/n)"
            if ($confirm -ne "j" -and $confirm -ne "J") {
                Write-Host "Abgebrochen." -ForegroundColor Red
                return
            }
        }
    } elseif ($choice -eq "0") {
        Write-Host "Abgebrochen." -ForegroundColor Yellow
        return
    }
    
    # Push
    Write-Host "`nPushe zu GitHub (origin/$targetBranch)..." -ForegroundColor Cyan
    git push origin HEAD:$targetBranch
    
    if ($LASTEXITCODE -eq 0) {
        Write-Host "`nErfolgreich zu '$targetBranch' gepusht!" -ForegroundColor Green
    } else {
        Write-Host "`nFehler beim Pushen!" -ForegroundColor Red
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
    } elseif ($Push) {
        Push-Branch
    } else {
        # Interaktiver Modus
        Show-MainMenu
    }
    
} catch {
    Write-Host "`nFehler: $_" -ForegroundColor Red
    exit 1
}
