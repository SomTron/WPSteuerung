from flask import Flask, render_template, request, redirect
import configparser

app = Flask(__name__)

CONFIG_FILE = "config.ini"

# Funktion zum Laden der Konfiguration
def load_config():
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)
    return config

# Funktion zum Speichern der Konfiguration
def save_config(config):
    with open(CONFIG_FILE, "w") as configfile:
        config.write(configfile)

@app.route("/", methods=["GET", "POST"])
def index():
    config = load_config()

    if request.method == "POST":
        # Werte aus dem Formular lesen und in Integer umwandeln
        ausschalttemperatur = int(request.form["AUSSCHALTPUNKT"])
        ausschalttemperatur_erhoeht = int(request.form["AUSSCHALTPUNKT_ERHOEHT"])
        einschalttemperatur = int(request.form["EINSCHALTPUNKT"])
        min_laufzeit = int(request.form["MIN_LAUFZEIT"])
        min_pause = int(request.form["MIN_PAUSE"])
        nachtabsenkung_start = request.form["NACHTABSENKUNG_START"]
        nachtabsenkung_end = request.form["NACHTABSENKUNG_END"]
        nachtabsenkung = int(request.form["NACHTABSENKUNG"])

        # Werte auf maximal 70 begrenzen
        ausschalttemperatur = min(ausschalttemperatur, 70)
        ausschalttemperatur_erhoeht = min(ausschalttemperatur_erhoeht, 70)
        einschalttemperatur = min(einschalttemperatur, 70)
        nachtabsenkung = min(nachtabsenkung, 70)

        # Werte in der Konfiguration speichern
        config["Heizungssteuerung"]["AUSSCHALTPUNKT"] = str(ausschalttemperatur)
        config["Heizungssteuerung"]["AUSSCHALTPUNKT_ERHOEHT"] = str(ausschalttemperatur_erhoeht)
        config["Heizungssteuerung"]["EINSCHALTPUNKT"] = str(einschalttemperatur)
        config["Heizungssteuerung"]["MIN_LAUFZEIT"] = str(min_laufzeit)
        config["Heizungssteuerung"]["MIN_PAUSE"] = str(min_pause)
        config["Heizungssteuerung"]["NACHTABSENKUNG_START"] = nachtabsenkung_start
        config["Heizungssteuerung"]["NACHTABSENKUNG_END"] = nachtabsenkung_end
        config["Heizungssteuerung"]["NACHTABSENKUNG"] = str(nachtabsenkung)

        save_config(config)
        return redirect("/")  # Seite nach Änderung neu laden

    return render_template("index.html", config=config["Heizungssteuerung"])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)