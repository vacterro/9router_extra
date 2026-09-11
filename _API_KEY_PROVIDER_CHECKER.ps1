# LLM_API_CHECKER.ps1
# PowerShell 5.1 compatible
# Interactive checker for OpenAI-compatible LLM APIs.

$ErrorActionPreference = "Stop"

# PowerShell 5.1 may otherwise negotiate old TLS on some systems.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Providers = [ordered]@{
    "1" = @{
        Name    = "Vikasit AI"
        BaseUrl = "https://api.vikasit.ai/v1"
        Model   = "vikasit-nova"
    }

    "2" = @{
        Name    = "MegaBrain"
        BaseUrl = "https://getmegabrain.com/api/gateway/v1"
        Model   = "auto-free"
    }

    "3" = @{
        Name    = "BazaarLink"
        BaseUrl = "https://api.bazaarlink.ai/v1"
        Model   = "auto:free"
    }

    "4" = @{
        Name    = "SimpleLLM"
        BaseUrl = "https://api.simplellm.eu/v1"
        Model   = "Mistral-7B"
    }
}

function Write-Header {
    Clear-Host

    Write-Host ""
    Write-Host "=============================================="
    Write-Host "        OPENAI-COMPATIBLE API CHECKER"
    Write-Host "=============================================="
    Write-Host ""
}

function ConvertTo-PlainText {
    param(
        [Parameter(Mandatory = $true)]
        [Security.SecureString]$SecureString
    )

    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureString)

    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

function Get-HttpErrorInfo {
    param(
        [Parameter(Mandatory = $true)]
        $Exception
    )

    $statusCode = $null
    $body = $null

    try {
        if ($Exception.Response) {
            $statusCode = [int]$Exception.Response.StatusCode

            $stream = $Exception.Response.GetResponseStream()

            if ($stream) {
                $reader = New-Object System.IO.StreamReader($stream)

                try {
                    $body = $reader.ReadToEnd()
                }
                finally {
                    $reader.Dispose()
                }
            }
        }
    }
    catch {
    }

    return @{
        StatusCode = $statusCode
        Body       = $body
        Message    = $Exception.Message
    }
}

function Show-HttpClassification {
    param(
        $StatusCode,
        [string]$Body,
        [string]$Message
    )

    switch ($StatusCode) {
        400 {
            Write-Host "STATUS: REQUEST REJECTED"
            Write-Host "HTTP:   400 Bad Request"
        }

        401 {
            Write-Host "STATUS: AUTH FAILED"
            Write-Host "HTTP:   401 Unauthorized"
            Write-Host "KEY:    invalid / expired / wrong auth format"
        }

        402 {
            Write-Host "STATUS: PAYMENT / BALANCE REQUIRED"
            Write-Host "HTTP:   402"
        }

        403 {
            Write-Host "STATUS: ACCESS FORBIDDEN"
            Write-Host "HTTP:   403"
            Write-Host "KEY may be valid, but access is denied."
        }

        404 {
            Write-Host "STATUS: ENDPOINT OR MODEL NOT FOUND"
            Write-Host "HTTP:   404"
        }

        429 {
            Write-Host "STATUS: RATE LIMIT / QUOTA EXHAUSTED"
            Write-Host "HTTP:   429"
            Write-Host "Provider is reachable; quota or rate limit blocked the request."
        }

        500 {
            Write-Host "STATUS: PROVIDER SERVER ERROR"
            Write-Host "HTTP:   500"
        }

        502 {
            Write-Host "STATUS: PROVIDER / UPSTREAM ERROR"
            Write-Host "HTTP:   502"
        }

        503 {
            Write-Host "STATUS: PROVIDER TEMPORARILY UNAVAILABLE"
            Write-Host "HTTP:   503"
        }

        504 {
            Write-Host "STATUS: PROVIDER TIMEOUT"
            Write-Host "HTTP:   504"
        }

        default {
            if ($StatusCode) {
                Write-Host "STATUS: HTTP FAILURE"
                Write-Host "HTTP:   $StatusCode"
            }
            else {
                Write-Host "STATUS: CONNECTION FAILURE"
            }
        }
    }

    if ($Message) {
        Write-Host ""
        Write-Host "ERROR:"
        Write-Host $Message
    }

    if ($Body) {
        Write-Host ""
        Write-Host "RESPONSE:"
        Write-Host $Body
    }
}

function Test-ModelsEndpoint {
    param(
        [string]$BaseUrl,
        [hashtable]$Headers
    )

    $url = "$($BaseUrl.TrimEnd('/'))/models"

    Write-Host ""
    Write-Host "[1/2] Checking models endpoint..."
    Write-Host "GET $url"

    try {
        $result = Invoke-RestMethod `
            -Method Get `
            -Uri $url `
            -Headers $Headers `
            -TimeoutSec 20

        Write-Host "PASS: /models responded."

        $ids = @()

        if ($result.data) {
            foreach ($item in $result.data) {
                if ($item.id) {
                    $ids += [string]$item.id
                }
            }
        }

        if ($ids.Count -gt 0) {
            Write-Host ""
            Write-Host "Models returned: $($ids.Count)"

            $ids |
                Select-Object -First 15 |
                ForEach-Object {
                    Write-Host "  $_"
                }

            if ($ids.Count -gt 15) {
                Write-Host "  ... plus $($ids.Count - 15) more"
            }
        }

        return @{
            Ok     = $true
            Models = $ids
        }
    }
    catch {
        $info = Get-HttpErrorInfo $_.Exception

        Write-Host "WARN: /models failed."

        Show-HttpClassification `
            -StatusCode $info.StatusCode `
            -Body $info.Body `
            -Message $info.Message

        # /models is useful, but some compatible providers don't expose it.
        return @{
            Ok     = $false
            Models = @()
        }
    }
}

function Test-ChatCompletion {
    param(
        [string]$BaseUrl,
        [string]$Model,
        [hashtable]$Headers
    )

    $url = "$($BaseUrl.TrimEnd('/'))/chat/completions"

    Write-Host ""
    Write-Host "[2/2] Checking chat completion..."
    Write-Host "POST $url"
    Write-Host "Model: $Model"

    $payload = @{
        model = $Model

        messages = @(
            @{
                role    = "user"
                content = "Reply with exactly: API_CHECK_OK"
            }
        )

        temperature = 0
        max_tokens  = 16
        stream      = $false
    }

    $json = $payload | ConvertTo-Json -Depth 10

    try {
        $response = Invoke-RestMethod `
            -Method Post `
            -Uri $url `
            -Headers $Headers `
            -Body $json `
            -ContentType "application/json" `
            -TimeoutSec 45

        $content = $null

        try {
            $content = $response.choices[0].message.content
        }
        catch {
        }

        Write-Host ""
        Write-Host "=============================================="
        Write-Host "RESULT: PROVIDER IS LIVE"
        Write-Host "=============================================="
        Write-Host ""
        Write-Host "HTTP completion: PASS"
        Write-Host "Model:           $Model"

        if ($content) {
            Write-Host "Response:        $content"
        }
        else {
            Write-Host "Response object received, but no normal text content was found."
        }

        if ($response.usage) {
            Write-Host ""
            Write-Host "Usage:"

            if ($null -ne $response.usage.prompt_tokens) {
                Write-Host "  Prompt tokens:     $($response.usage.prompt_tokens)"
            }

            if ($null -ne $response.usage.completion_tokens) {
                Write-Host "  Completion tokens: $($response.usage.completion_tokens)"
            }

            if ($null -ne $response.usage.total_tokens) {
                Write-Host "  Total tokens:      $($response.usage.total_tokens)"
            }
        }

        return $true
    }
    catch {
        $info = Get-HttpErrorInfo $_.Exception

        Write-Host ""
        Write-Host "=============================================="
        Write-Host "RESULT: COMPLETION FAILED"
        Write-Host "=============================================="
        Write-Host ""

        Show-HttpClassification `
            -StatusCode $info.StatusCode `
            -Body $info.Body `
            -Message $info.Message

        return $false
    }
}

function Test-Provider {
    param(
        [string]$Name,
        [string]$BaseUrl,
        [string]$Model
    )

    Write-Header

    Write-Host "Provider: $Name"
    Write-Host "Base URL: $BaseUrl"
    Write-Host "Model:    $Model"
    Write-Host ""

    $secureKey = Read-Host "Paste API key" -AsSecureString

    if ($secureKey.Length -eq 0) {
        Write-Host ""
        Write-Host "No API key entered."
        return
    }

    $apiKey = ConvertTo-PlainText $secureKey

    try {
        $headers = @{
            Authorization = "Bearer $apiKey"
            Accept        = "application/json"
        }

        $modelsResult = Test-ModelsEndpoint `
            -BaseUrl $BaseUrl `
            -Headers $headers

        $chatOk = Test-ChatCompletion `
            -BaseUrl $BaseUrl `
            -Model $Model `
            -Headers $headers

        Write-Host ""
        Write-Host "----------------------------------------------"
        Write-Host "SUMMARY"
        Write-Host "----------------------------------------------"
        Write-Host "Provider:   $Name"
        Write-Host "Base URL:   $BaseUrl"
        Write-Host "Model:      $Model"

        if ($modelsResult.Ok) {
            Write-Host "Models API: PASS"
        }
        else {
            Write-Host "Models API: WARN/FAIL"
        }

        if ($chatOk) {
            Write-Host "Completion: PASS"
            Write-Host ""
            Write-Host "FINAL: LIVE / USABLE"
        }
        else {
            Write-Host "Completion: FAIL"
            Write-Host ""
            Write-Host "FINAL: NOT CURRENTLY USABLE"
        }
    }
    finally {
        # Don't deliberately preserve the plaintext key longer than needed.
        $apiKey = $null
        $secureKey = $null
        $headers = $null
    }
}

while ($true) {
    Write-Header

    Write-Host "Choose provider:"
    Write-Host ""
    Write-Host "  1. Vikasit AI"
    Write-Host "  2. MegaBrain"
    Write-Host "  3. BazaarLink"
    Write-Host "  4. SimpleLLM"
    Write-Host "  M. Manual OpenAI-compatible provider"
    Write-Host ""
    Write-Host "  Q. Quit"
    Write-Host ""

    $choice = (Read-Host "Provider").Trim()

    if ($choice -match '^[Qq]$') {
        break
    }

    if ($choice -match '^[Mm]$') {
        Write-Header

        $manualName = Read-Host "Provider name"
        $manualBase = Read-Host "Base URL, e.g. https://api.example.com/v1"
        $manualModel = Read-Host "Model ID"

        if (
            [string]::IsNullOrWhiteSpace($manualName) -or
            [string]::IsNullOrWhiteSpace($manualBase) -or
            [string]::IsNullOrWhiteSpace($manualModel)
        ) {
            Write-Host ""
            Write-Host "Provider name, Base URL and Model are required."
            Write-Host ""
            Read-Host "Press ENTER"
            continue
        }

        Test-Provider `
            -Name $manualName `
            -BaseUrl $manualBase `
            -Model $manualModel
    }
    elseif ($Providers.Contains($choice)) {
        $provider = $Providers[$choice]

        Test-Provider `
            -Name $provider.Name `
            -BaseUrl $provider.BaseUrl `
            -Model $provider.Model
    }
    else {
        Write-Host ""
        Write-Host "Unknown selection."
    }

    Write-Host ""
    Write-Host "=============================================="
    Read-Host "Press ENTER to test another provider"
}