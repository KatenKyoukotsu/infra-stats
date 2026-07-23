package health

import (
	"fmt"
	"net/http"
)

// Handler отвечает за проверку работоспособности самого сервиса мониторинга
func Handler(w http.ResponseWriter, r *http.Request) {
	// Для Prometheus можно отдавать простой 200 OK
	w.WriteHeader(http.StatusOK)
	w.Header().Set("Content-Type", "text/plain")
	fmt.Fprintln(w, "ItsOk")
}