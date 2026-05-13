$env:PYTHONUNBUFFERED="1"

Write-Host "Starting Overnight Benchmark Suite with gemma4:e4b"
Write-Host "=================================================="

Write-Host "1. Running Baseline (Off Mode)..."
python benchmarks/bench_aa_suite.py --benchmark scicode --mode off --model gemma4:e4b --output benchmarks/scicode_overnight_off.json

Write-Host "2. Running Treatment (On Mode)..."
python benchmarks/bench_aa_suite.py --benchmark scicode --mode on --model gemma4:e4b --output benchmarks/scicode_overnight_on.json

Write-Host "3. Generating Comparison Report..."
python benchmarks/compare_ab.py benchmarks/scicode_overnight_off.json benchmarks/scicode_overnight_on.json > benchmarks/scicode_overnight_report.txt

Write-Host "Overnight benchmark complete! Report saved to benchmarks/scicode_overnight_report.txt"
