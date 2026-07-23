package web

import (
	"encoding/json"
	"net/http"

	"ssh-checker/internal/checker"
	"ssh-checker/internal/config"
	"ssh-checker/internal/notifier"
	"ssh-checker/internal/storage"
)

type API struct {
	cfgMgr   *config.Manager
	engine   *checker.Engine
	store    *storage.Storage
	notifier *notifier.Client
}

func NewAPI(
	cfgMgr *config.Manager,
	engine *checker.Engine,
	store *storage.Storage,
	notifier *notifier.Client,
) *API {
	return &API{
		cfgMgr:   cfgMgr,
		engine:   engine,
		store:    store,
		notifier: notifier,
	}
}

// GetStatusHandler возвращает последний отчет
func (a *API) GetStatusHandler(w http.ResponseWriter, r *http.Request) {
	report, ok := a.store.GetLastReport()
	if !ok {
		// Если проверок ещё не было, возвращаем пустой ответ
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"message": "No checks performed yet",
			"report":  nil,
		})
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(report)
}

// RunCheckHandler запускает проверку по кнопке из UI
func (a *API) RunCheckHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	cfg := a.cfgMgr.Get()
	report := a.engine.RunCheck(cfg.Targets)
	a.store.AddReport(report)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(report)
}

// SendReportHandler триггерит отправку отчета в BotX вручную
func (a *API) SendReportHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	report, ok := a.store.GetLastReport()
	if !ok {
		http.Error(w, "No check report available to send", http.StatusBadRequest)
		return
	}

	cfg := a.cfgMgr.Get()
	if err := a.notifier.SendReport(cfg.Messenger, report); err != nil {
		http.Error(w, "Failed to send report: "+err.Error(), http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "success", "message": "Report sent to BotX"})
}

// ClearStorageHandler очищает историю
func (a *API) ClearStorageHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	a.store.Clear()
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "success", "message": "Storage cleared"})
}

// GetConfigHandler возвращает текущий конфиг
func (a *API) GetConfigHandler(w http.ResponseWriter, r *http.Request) {
	cfg := a.cfgMgr.Get()
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(cfg)
}

// UpdateConfigHandler перезаписывает конфиг и сохраняет в YAML
func (a *API) UpdateConfigHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var newCfg config.Config
	if err := json.NewDecoder(r.Body).Decode(&newCfg); err != nil {
		http.Error(w, "Invalid JSON body: "+err.Error(), http.StatusBadRequest)
		return
	}

	if err := a.cfgMgr.Save(&newCfg); err != nil {
		http.Error(w, "Failed to save config: "+err.Error(), http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "success", "message": "Config updated and saved to YAML"})
}