# build_lambda.ps1 — rebuild the SAM deployment artifact in package/
# Run this before `sam deploy` whenever Lambda source changes.
# NOTE: pip installs platform-specific wheels. For Lambda (Amazon Linux 2),
# either run this inside a Docker container or use `sam build` with --use-container.

param(
    [string]$PythonCmd = "python"
)

$ErrorActionPreference = "Stop"
$PackageDir = Join-Path $PSScriptRoot "package"

Write-Host "Cleaning $PackageDir ..."
if (Test-Path $PackageDir) { Remove-Item -Recurse -Force $PackageDir }
New-Item -ItemType Directory -Path $PackageDir | Out-Null

Write-Host "Installing Lambda pip dependencies ..."
& $PythonCmd -m pip install -t $PackageDir `
    "anthropic>=0.50.0" `
    "httpx>=0.27.0" `
    "boto3>=1.35.0" `
    "python-dotenv>=1.0.0"

Write-Host "Copying bgg_shared ..."
Copy-Item -Recurse -Force (Join-Path $PSScriptRoot "packages\shared\bgg_shared") `
                          (Join-Path $PackageDir "bgg_shared")

Write-Host "Copying bgg_lambda ..."
Copy-Item -Recurse -Force (Join-Path $PSScriptRoot "packages\lambda_handler\bgg_lambda") `
                          (Join-Path $PackageDir "bgg_lambda")

Write-Host "Copying SAM entry-point shim ..."
Copy-Item -Force (Join-Path $PSScriptRoot "packages\lambda_handler\lambda_function.py") `
                 (Join-Path $PackageDir "lambda_function.py")

Write-Host "Done. Deploy with: sam deploy --parameter-overrides ..."
