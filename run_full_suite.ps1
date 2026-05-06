$env:PYTHONUNBUFFERED="1"

Write-Host "Running IFBench..."
python benchmarks/bench_aa_suite.py --benchmark ifbench --mode off --model gemma4:e4b --output benchmarks/ifbench_off.json
python benchmarks/bench_aa_suite.py --benchmark ifbench --mode on --model gemma4:e4b --output benchmarks/ifbench_on.json
Write-Host "--- IFBench Results ---" > benchmarks/full_suite_report.txt
python benchmarks/compare_ab.py benchmarks/ifbench_off.json benchmarks/ifbench_on.json >> benchmarks/full_suite_report.txt

Write-Host "Running AA-Omniscience..."
python benchmarks/bench_aa_suite.py --benchmark aa-omniscience --mode off --model gemma4:e4b --output benchmarks/omn_off.json
python benchmarks/bench_aa_suite.py --benchmark aa-omniscience --mode on --model gemma4:e4b --output benchmarks/omn_on.json
Write-Host "`n--- AA-Omniscience Results ---" >> benchmarks/full_suite_report.txt
python benchmarks/compare_ab.py benchmarks/omn_off.json benchmarks/omn_on.json >> benchmarks/full_suite_report.txt

Write-Host "Running CritPt..."
python benchmarks/bench_aa_suite.py --benchmark critpt --mode off --model gemma4:e4b --output benchmarks/critpt_off.json
python benchmarks/bench_aa_suite.py --benchmark critpt --mode on --model gemma4:e4b --output benchmarks/critpt_on.json
Write-Host "`n--- CritPt Results ---" >> benchmarks/full_suite_report.txt
python benchmarks/compare_ab.py benchmarks/critpt_off.json benchmarks/critpt_on.json >> benchmarks/full_suite_report.txt

Write-Host "Full suite complete! Report saved to benchmarks/full_suite_report.txt"
