# Gemeinsame Hilfsfunktionen fuer den SSH-Agent.
#
# Problem auf dem Pi: Nach einem Reboot laeuft kein ssh-agent. Ein
# 'ssh-add' scheitert dann mit "Could not open a connection to your
# authentication agent" und JEDER Deploy bricht mit
# "Permission denied (publickey)" ab - auch wenn der Key auf der Platte
# liegt. Der ssh-agent laeuft naemlich nur, wenn er beim Login per
# 'eval $(ssh-agent -s)' gestartet wird (also aus der .bashrc).
#
# Diese Datei startet den Agent bei Bedarf und laedt den Key nach. Wird
# von wp-manager-menu.sh und rpi-deploy.sh eingebunden (mit '.'), damit
# beide Wege - Menue und direkter Deploy-Aufruf - gleich gut funktionieren.
#
# Ist der Key mit einer Passphrase gesichert, fragt 'ssh-add' sie
# interaktiv ab. Das geht nur an einem Terminal - ohne TTY (z. B. beim
# nicht-interaktiven 'wps --deploy') wird die Abhilfe gemeldet, statt
# zu blockieren.

SSH_AGENT_HILFE=0

# Gibt 0 zurueck, wenn danach mindestens ein Key im Agent liegt.
sorge_fuer_ssh_agent() {
    # Bereits ein Key geladen? Dann nichts tun.
    if [ -n "${SSH_AUTH_SOCK:-}" ] && ssh-add -l >/dev/null 2>&1; then
        return 0
    fi

    _key_datei="${WPS_SSH_KEY:-$HOME/.ssh/id_ed25519}"
    if [ ! -f "$_key_datei" ]; then
        # Kein Key vorhanden - nichts zu tun (nicht jeder hat einen).
        return 1
    fi

    # Agent starten, falls keiner laeuft. Die Ausgabe geht in die
    # Shell-Umgebung (eval), damit SSH_AUTH_SOCK gesetzt wird.
    if [ -z "${SSH_AUTH_SOCK:-}" ] || ! ssh-add -l >/dev/null 2>&1; then
        if ! command -v ssh-agent >/dev/null 2>&1; then
            return 1
        fi
        _agent_out=$(ssh-agent -s 2>/dev/null) || return 1
        if [ -z "$_agent_out" ]; then
            return 1
        fi
        eval "$_agent_out" >/dev/null 2>&1 || return 1
    fi

    # Key nachladen. Bei Passphrase wird hier interaktiv gefragt - das
    # setzt ein TTY voraus.
    if ssh-add "$_key_datei" 2>/dev/null; then
        return 0
    fi

    # Ohne TTY kann die Passphrase nicht abgefragt werden.
    if [ ! -t 0 ]; then
        printf '%s\n' "Hinweis: SSH-Key konnte nicht geladen werden (kein Terminal fuer die Passphrase)." >&2
        printf '%s\n' "        Abhilfe: einmalig im Terminal  ssh-add ~/.ssh/id_ed25519" >&2
        SSH_AGENT_HILFE=1
        return 1
    fi

    printf '%s\n' "Hinweis: ssh-add wurde abgebrochen - der Key ist dann nicht geladen." >&2
    printf '%s\n' "        Abhilfe: einmalig im Terminal  ssh-add ~/.ssh/id_ed25519" >&2
    SSH_AGENT_HILFE=1
    return 1
}

# Kurzstatus fuer die Anzeige: 1 = Key geladen, 0 = keiner.
ssh_key_geladen() {
    [ -n "${SSH_AUTH_SOCK:-}" ] && ssh-add -l >/dev/null 2>&1
}
