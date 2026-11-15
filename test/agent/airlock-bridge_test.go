// Loaded with go -overlay into the pinned Airlock interactionapi test package.
// Reuses its real DynamoDB, signed OIDC and owner fixtures; no production changes.
package interactionapi

import (
	"bufio"
	"encoding/json"
	"fmt"
	"github.com/lunareng/airlock/internal/store"
	"github.com/lunareng/airlock/internal/store/storetest"
	"os"
	"os/exec"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestActionsBridgeRoundTrip(t *testing.T) {
	for _, scenario := range []string{"success", "before-write", "after-write", "lost-http", "changed-id", "owner-demoted", "revoked", "expired"} {
		t.Run(scenario, func(t *testing.T) {
			f := fresh(t)
			cmd := exec.Command("node", os.Getenv("AIRLOCK_BRIDGE_DRIVER"))
			in, err := cmd.StdinPipe()
			if err != nil {
				t.Fatal(err)
			}
			out, err := cmd.StdoutPipe()
			if err != nil {
				t.Fatal(err)
			}
			cmd.Stderr = os.Stderr
			if err = cmd.Start(); err != nil {
				t.Fatal(err)
			}
			defer cmd.Process.Kill()
			encoder := json.NewEncoder(in)
			if err = encoder.Encode(map[string]any{"native": f.native, "scenario": scenario}); err != nil {
				t.Fatal(err)
			}
			scanner := bufio.NewScanner(out)
			scanner.Buffer(make([]byte, 4096), 262144)
			first, done := true, false
			for scanner.Scan() {
				var msg struct {
					Body       runInput `json:"body"`
					Done       bool     `json:"done"`
					Writes     int      `json:"writes"`
					Deliveries int      `json:"deliveries"`
				}
				if err = json.Unmarshal(scanner.Bytes(), &msg); err != nil {
					t.Fatal(err)
				}
				if msg.Done {
					done = true
					if msg.Deliveries > 1 {
						t.Fatal("duplicate delivery")
					}
					break
				}
				if msg.Body.Operation == "deliver" {
					switch scenario {
					case "owner-demoted":
						f.db.setMemberRole(t, "acme", storetest.AcmeOwner, store.RoleViewer)
					case "revoked":
						if _, err = f.scope.MarkRevoked(bg, f.row.Ref(), store.ISOMS(f.now)); err != nil {
							t.Fatal(err)
						}
					case "expired":
						f.now = f.now.Add(time.Hour)
					}
				}
				w := f.run(t, msg.Body)
				if first {
					mustStatus(t, w, 200)
					var q questionView
					if err = json.Unmarshal(w.Body.Bytes(), &q); err != nil {
						t.Fatal(err)
					}
					f.restart(t)
					mustStatus(t, f.answer(t, q.Request.ID, command(), f.owner()), 200)
					first = false
				}
				// Recreate the API/backend; only DynamoDB state survives between calls.
				f.restart(t)
				reply := map[string]any{"value": json.RawMessage(w.Body.Bytes())}
				if w.Code != 200 || (scenario == "lost-http" && msg.Body.Operation == "deliver") {
					reply = map[string]any{"error": true}
				}
				if err = encoder.Encode(reply); err != nil {
					t.Fatal(err)
				}
			}
			in.Close()
			if err = scanner.Err(); err != nil {
				t.Fatal(err)
			}
			if err = cmd.Wait(); err != nil {
				t.Fatal(err)
			}
			if !done {
				t.Fatal("bridge did not report completion")
			}
			history := f.request(t, "GET", "/v1/t/acme/runs/1001.1/interactions", nil, "", f.owner())
			if scenario == "owner-demoted" {
				if history.Code != 403 {
					t.Fatal("demoted owner could read private history")
				}
			} else {
				mustStatus(t, history, 200)
				expected := map[string]string{"success": "resolved_not_applied_proof", "before-write": "accepted", "after-write": "delivery_unknown", "lost-http": "delivery_unknown", "changed-id": "accepted"}[scenario]
				if expected != "" && !strings.Contains(history.Body.String(), expected) {
					t.Fatalf("retained recovery state missing %s", expected)
				}
			}

			row, err := f.scope.Run(bg, f.row.ID)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(row.Grant, f.row.Grant) || !reflect.DeepEqual(row.Issued, f.row.Issued) || row.ReadIssued != nil || len(row.Sessions) != 0 {
				t.Fatal("bridge expanded run credentials")
			}
			fmt.Println("bridge proof:", scenario, "grant unchanged")
		})
	}
}
