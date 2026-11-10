$env:PYTHONUNBUFFERED="1"

Write-Host "Running PR #9 Benchmark Sweep with Query Classifier..."
Write-Host "======================================================"

Write-Host "`nRunning AA-LCR (Multi-Hop Test)..."
python benchmarks/bench_aa_suite.py --benchmark aa-lcr --mode off --model gemma4:e4b --output benchmarks/pr9_aalcr_off.json
python benchmarks/bench_aa_suite.py --benchmark aa-lcr --mode on --model gemma4:e4b --output benchmarks/pr9_aalcr_on.json
Write-Host "--- AA-LCR Results ---" > benchmarks/pr9_sweep_report.txt
python benchmarks/compare_ab.py benchmarks/pr9_aalcr_off.json benchmarks/pr9_aalcr_on.json >> benchmarks/pr9_sweep_report.txt

Write-Host "`nRunning Terminal-Bench (Log Parsing Test)..."
python benchmarks/bench_aa_suite.py --benchmark terminal-bench --mode off --model gemma4:e4b --output benchmarks/pr9_term_off.json
python benchmarks/bench_aa_suite.py --benchmark terminal-bench --mode on --model gemma4:e4b --output benchmarks/pr9_term_on.json
Write-Host "`n--- Terminal-Bench Results ---" >> benchmarks/pr9_sweep_report.txt
python benchmarks/compare_ab.py benchmarks/pr9_term_off.json benchmarks/pr9_term_on.json >> benchmarks/pr9_sweep_report.txt

Write-Host "`nRunning CritPt (Planning Test)..."
python benchmarks/bench_aa_suite.py --benchmark critpt --mode off --model gemma4:e4b --output benchmarks/pr9_crit_off.json
python benchmarks/bench_aa_suite.py --benchmark critpt --mode on --model gemma4:e4b --output benchmarks/pr9_crit_on.json
Write-Host "`n--- CritPt Results ---" >> benchmarks/pr9_sweep_report.txt
python benchmarks/compare_ab.py benchmarks/pr9_crit_off.json benchmarks/pr9_crit_on.json >> benchmarks/pr9_sweep_report.txt

Write-Host "`nSweep complete! Report saved to benchmarks/pr9_sweep_report.txt"
