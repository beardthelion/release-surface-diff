package hidden

import "net/http"

func Hidden(w http.ResponseWriter, r *http.Request) {}

func Register() {
	http.HandleFunc("/hidden", Hidden)
}
