#!/usr/bin/env python3
"""Add legionellen lifecycle management to main.py."""
with open('main.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add legionellen planning in check_periodic_tasks after Sommer-Modus evaluation
old1 = """                elif ereignis == SOMMER_DEAKTIVIERT_DATEN:
                    logging.info("Sommer-Modus INAKTIV: Keine vollstaendigen Prognosedaten verfuegbar")

    return last_vpn_check"""

new1 = """                elif ereignis == SOMMER_DEAKTIVIERT_DATEN:
                    logging.info("Sommer-Modus INAKTIV: Keine vollstaendigen Prognosedaten verfuegbar")

            # --- Legionellenprophylaxe Planung (nach Prognose-Update) ---
            legionellen_cfg = state.priority_config.legionellen
            if legionellen_cfg.aktiv:
                aktuelle_kw = now_local.isocalendar()[1]
                letzte_kw = None
                if state.legionellen_last_done is not None:
                    letzte_kw = state.legionellen_last_done.isocalendar()[1]

                # Nur planen, wenn nicht bereits in dieser KW erledigt
                if letzte_kw != aktuelle_kw or state.legionellen_last_done is None:
                    aktueller_wochentag = now_local.weekday()

                    # Verfuegbare Tage zwischen bevorzugt und letztem Tag
                    verfuegbare_tage = []
                    for tag in range(legionellen_cfg.bevorzugter_tag, legionellen_cfg.letzter_tag + 1):
                        if tag >= aktueller_wochentag:
                            verfuegbare_tage.append(tag)

                    if verfuegbare_tage:
                        tages_prognose = {}
                        for offset, tag_idx in [(0, aktueller_wochentag),
                                                 (1, (aktueller_wochentag + 1) % 7),
                                                 (2, (aktueller_wochentag + 2) % 7)]:
                            if offset == 0 and rad_today is not None:
                                tages_prognose[tag_idx] = rad_today
                            elif offset == 1 and rad_tomorrow is not None:
                                tages_prognose[tag_idx] = rad_tomorrow
                            elif offset == 2 and rad_day2 is not None:
                                tages_prognose[tag_idx] = rad_day2

                        bester_tag = legionellen_cfg.bevorzugter_tag
                        beste_prognose = tages_prognose.get(bester_tag, 0.0)
                        bester_grund = "Bevorzugter Tag"

                        for tag in verfuegbare_tage:
                            if tag == legionellen_cfg.bevorzugter_tag:
                                continue
                            prognose = tages_prognose.get(tag, 0.0)
                            if (prognose - beste_prognose) >= legionellen_cfg.erforderliche_wh_qm:
                                bester_tag = tag
                                beste_prognose = prognose
                                bester_grund = f"Bessere PV-Prognose ({prognose:.0f} Wh/qm)"
                            elif (prognose >= legionellen_cfg.pv_prognose_schwelle_gut and
                                  beste_prognose < legionellen_cfg.pv_prognose_schwelle_gut):
                                bester_tag = tag
                                beste_prognose = prognose
                                bester_grund = f"Gute PV-Prognose am Alternativtag ({prognose:.0f} Wh/qm)"

                        from priority_control import _wochentag_name
                        state.legionellen_planned_day = _wochentag_name(bester_tag)
                        state.legionellen_planned_time = f"{legionellen_cfg.start_uhr}:00"
                        state.legionellen_planned_reason = bester_grund

    return last_vpn_check"""

if old1 in content:
    content = content.replace(old1, new1)
    print('OK: check_periodic_tasks updated')
else:
    print('FAILED: check_periodic_tasks')
    idx = content.find('SOMMER_DEAKTIVIERT_DATEN')
    if idx >= 0:
        print('Found at', idx)
        print(repr(content[idx:idx+300]))

# 2. Add legionellen lifecycle tracking in run_logic_step after check_and_send_alerts
old2 = """            # 6. Sofort-Alarme pruefen
            await check_and_send_alerts(session, state)

def build_heizungsdaten_zeile(state):"""

new2 = """            # 6. Sofort-Alarme pruefen
            await check_and_send_alerts(session, state)

            # 7. Legionellenprophylaxe Lifecycle-Tracking
            legionellen_cfg_lc = state.priority_config.legionellen
            if legionellen_cfg_lc.aktiv:
                gewinner_lc = result.get("gewinner_ergebnis")
                if gewinner_lc is not None and gewinner_lc.name == "Legionellen":
                    if gewinner_lc.einschalten is True and not state.legionellen_aktiv:
                        # Start der Prophylaxe
                        state.legionellen_aktiv = True
                        state.legionellen_started_at = datetime.now(state.local_tz)
                        state.legionellen_telegram_start_sent = False
                        state.legionellen_telegram_done_sent = False
                        state.legionellen_temp_override = legionellen_cfg_lc.legionellen_max_temp_c
                        state.legionellen_target_reached_at = None
                        logging.info(
                            f"Legionellenprophylaxe GESTARTET: Heize auf "
                            f"{legionellen_cfg_lc.target_temp_c:.0f}C (max {legionellen_cfg_lc.legionellen_max_temp_c:.0f}C)"
                        )
                        # Telegram-Benachrichtigung
                        try:
                            msg = (f"🦠 *Legionellenprophylaxe gestartet!*\n"
                                   f"Heize auf {legionellen_cfg_lc.target_temp_c:.0f}°C "
                                   f"(unten: {state.sensors.t_unten:.1f}°C)")
                            from telegram_api import send_telegram_message as _send_tg
                            await _send_tg(session, state.config.Telegram.CHAT_ID, msg,
                                           state.config.Telegram.BOT_TOKEN, parse_mode="Markdown")
                            state.legionellen_telegram_start_sent = True
                        except Exception as e:
                            logging.warning(f"Legionellen-Telegram-Start fehlgeschlagen: {e}")

                    elif gewinner_lc.einschalten is True and state.legionellen_aktiv:
                        # Laufende Prophylaxe: Pruefen ob Ziel erreicht
                        if state.legionellen_target_reached_at is None:
                            t_unten_lc = state.sensors.t_unten
                            if t_unten_lc is not None and t_unten_lc >= legionellen_cfg_lc.target_temp_c:
                                state.legionellen_target_reached_at = datetime.now(state.local_tz)
                                logging.info(
                                    f"Legionellen: Zieltemperatur {legionellen_cfg_lc.target_temp_c:.0f}C "
                                    f"erreicht! Starte Probezeit ({legionellen_cfg_lc.probezeit_minuten}m)"
                                )
                        else:
                            # Probezeit abwarten
                            probezeit_ende = state.legionellen_target_reached_at + timedelta(
                                minutes=legionellen_cfg_lc.probezeit_minuten
                            )
                            if datetime.now(state.local_tz) >= probezeit_ende:
                                logging.info(
                                    f"Legionellen: Probezeit von {legionellen_cfg_lc.probezeit_minuten}m "
                                    f"erfolgreich abgeschlossen"
                                )

                    elif gewinner_lc.einschalten is False and state.legionellen_aktiv:
                        # Prophylaxe abschliessen
                        if state.legionellen_target_reached_at is not None:
                            probezeit_ende = state.legionellen_target_reached_at + timedelta(
                                minutes=legionellen_cfg_lc.probezeit_minuten
                            )
                            if datetime.now(state.local_tz) >= probezeit_ende:
                                state.legionellen_last_done = datetime.now(state.local_tz).date()
                                aktuelle_kw = datetime.now(state.local_tz).isocalendar()[1]
                                state.legionellen_wochennummer = aktuelle_kw
                                state.legionellen_aktiv = False
                                state.legionellen_temp_override = None
                                logging.info(
                                    f"Legionellenprophylaxe ABGESCHLOSSEN: "
                                    f"KW {aktuelle_kw}, Temp-Ziel {legionellen_cfg_lc.target_temp_c:.0f}C erreicht"
                                )
                                try:
                                    msg = (f"✅ *Legionellenprophylaxe abgeschlossen!*\n"
                                           f"KW {aktuelle_kw}: {legionellen_cfg_lc.target_temp_c:.0f}°C erreicht")
                                    from telegram_api import send_telegram_message as _send_tg
                                    await _send_tg(session, state.config.Telegram.CHAT_ID, msg,
                                                   state.config.Telegram.BOT_TOKEN, parse_mode="Markdown")
                                    state.legionellen_telegram_done_sent = True
                                except Exception as e:
                                    logging.warning(f"Legionellen-Telegram-Done fehlgeschlagen: {e}")
                        else:
                            # Abgebrochen ohne Zielerreichung
                            state.legionellen_aktiv = False
                            state.legionellen_temp_override = None
                            state.legionellen_started_at = None
                            state.legionellen_target_reached_at = None
                            logging.warning("Legionellenprophylaxe ABGEBROCHEN (Ziel nicht erreicht)")
                else:
                    # Keine Legionellen-Regel aktiv -> Override zuruecksetzen
                    if state.legionellen_temp_override is not None:
                        state.legionellen_temp_override = None
                        logging.debug("Legionellen-Temp-Override zurueckgesetzt")

def build_heizungsdaten_zeile(state):"""

if old2 in content:
    content = content.replace(old2, new2)
    print('OK: run_logic_step updated')
else:
    print('FAILED: run_logic_step')
    idx = content.find('6. Sofort-Alarme pruefen')
    if idx >= 0:
        print('Found at', idx)
        print(repr(content[idx:idx+300]))

with open('main.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Done!')