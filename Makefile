.PHONY: setup images deploy test simulate swap experiment compare forward-grafana forward-controller logs clean
setup:            ; ./scripts/setup.sh
images:           ; ./scripts/build-images.sh
deploy:           ; python3 scripts/gen_dashboard.py && kubectl apply -f manifests/
test:             ; python3 -m unittest discover -s tests -v
simulate:         ; for s in step ramp accel; do python3 analysis/simulate.py --scenario $$s; echo; done
swap:             ; ./scripts/swap-app.sh $(APP) '$(OVERRIDE)'
experiment:       ; ./scripts/run-experiment.sh $(CFG) $(SCEN) $(REP)
compare:          ; python3 analysis/analyze.py --compare results
forward-grafana:  ; kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80
forward-controller: ; kubectl -n autoscale-lab port-forward svc/dfc-controller 18080:8080
logs:             ; kubectl -n autoscale-lab logs deploy/dfc-controller -f
clean:            ; ./scripts/teardown.sh
