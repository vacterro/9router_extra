# ============================================================
# 9Router Quick Live Scanner
# Windows PowerShell 5.1 / WPF
#
# Golden Default UI
# Quick scan:
#   - 8 models in parallel
#   - 4 second fast probe
#   - timeout-only delayed retry up to 12 seconds
#   - only HTTP 200 inference results are displayed
#   - detects Chat Completions vs Responses API
# ============================================================

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName WindowsBase
Add-Type -AssemblyName System.Xaml
Add-Type -AssemblyName System.Net.Http
Add-Type -AssemblyName System.Management.Automation

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$MaxConcurrency = 8
$FastTimeoutSec = 4
$SlowTimeoutSec = 12

$KnownProviders = @{
    "vsllm"     = "https://vsllm.com"
    "seekai"    = "https://seekai.cc"
    "gorouter"  = "https://gorouter.app"
    "justwoker" = "https://api.justwoker.icu"
    "tabitoken" = "https://tabitoken.com"
}

function Resolve-ApiBase {
    param([string]$InputValue)

    $value = $InputValue.Trim()

    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Provider is empty."
    }

    $alias = $value.ToLowerInvariant()

    if ($KnownProviders.ContainsKey($alias)) {
        $value = $KnownProviders[$alias]
    }
    elseif ($value -notmatch "^https?://") {
        if ($value -match "\.") {
            $value = "https://$value"
        }
        else {
            throw "Unknown provider alias. Enter a domain or full URL."
        }
    }

    $value = $value.TrimEnd("/")

    $suffixes = @(
        "/v1/chat/completions",
        "/chat/completions",
        "/v1/responses",
        "/responses",
        "/v1/models",
        "/models"
    )

    foreach ($suffix in $suffixes) {
        if ($value.EndsWith($suffix, [StringComparison]::OrdinalIgnoreCase)) {
            $value = $value.Substring(0, $value.Length - $suffix.Length)
            break
        }
    }

    if (-not $value.EndsWith("/v1", [StringComparison]::OrdinalIgnoreCase)) {
        $value = "$value/v1"
    }

    return $value
}

$catalogScript = {
    param(
        [string]$ApiBase,
        [string]$ApiKey
    )

    $ErrorActionPreference = "Stop"

    function Invoke-CatalogRequest {
        $handler = $null
        $client = $null

        try {
            $handler = New-Object System.Net.Http.HttpClientHandler
            $client = New-Object System.Net.Http.HttpClient($handler)
            $client.Timeout = [TimeSpan]::FromSeconds(15)

            $request = New-Object System.Net.Http.HttpRequestMessage
            $request.Method = [System.Net.Http.HttpMethod]::Get
            $request.RequestUri = "$ApiBase/models"
            $request.Headers.UserAgent.ParseAdd("9Router-Quick-Scanner/2.0")
            $request.Headers.Authorization =
                New-Object System.Net.Http.Headers.AuthenticationHeaderValue(
                    "Bearer",
                    $ApiKey
                )

            $response = $client.SendAsync($request).GetAwaiter().GetResult()
            $content = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()

            return [PSCustomObject]@{
                StatusCode = [int]$response.StatusCode
                Content    = $content
            }
        }
        catch {
            return [PSCustomObject]@{
                StatusCode = 0
                Content    = $_.Exception.Message
            }
        }
        finally {
            if ($null -ne $client) {
                $client.Dispose()
            }

            if ($null -ne $handler) {
                $handler.Dispose()
            }
        }
    }

    function Test-IsLikelyTextModel {
        param([string]$ModelId)

        if ([string]::IsNullOrWhiteSpace($ModelId)) {
            return $false
        }

        $id = $ModelId.ToLowerInvariant()

        $deny = @(
            "embedding",
            "rerank",
            "re-rank",
            "whisper",
            "tts",
            "speech",
            "transcri",
            "dall-e",
            "image",
            "sora",
            "video",
            "moderation"
        )

        foreach ($item in $deny) {
            if ($id.Contains($item)) {
                return $false
            }
        }

        return $true
    }

    $response = Invoke-CatalogRequest

    if ($response.StatusCode -ne 200) {
        $detail = $response.Content -replace "\s+", " "
        $detail = $detail.Trim()

        if ($detail.Length -gt 240) {
            $detail = $detail.Substring(0, 240) + "..."
        }

        [PSCustomObject]@{
            Success = $false
            Status  = $response.StatusCode
            Error   = $detail
            Models  = @()
        }

        return
    }

    try {
        $json = $response.Content | ConvertFrom-Json
    }
    catch {
        [PSCustomObject]@{
            Success = $false
            Status  = 200
            Error   = "/v1/models returned invalid JSON."
            Models  = @()
        }

        return
    }

    if ($null -ne $json.data) {
        $rawModels = @($json.data)
    }
    elseif ($json -is [System.Array]) {
        $rawModels = @($json)
    }
    else {
        [PSCustomObject]@{
            Success = $false
            Status  = 200
            Error   = "Unsupported /v1/models response shape."
            Models  = @()
        }

        return
    }

    $ids = @(
        $rawModels |
        Where-Object {
            $null -ne $_.id -and
            (Test-IsLikelyTextModel -ModelId ([string]$_.id))
        } |
        ForEach-Object { [string]$_.id } |
        Sort-Object -Unique
    )

    [PSCustomObject]@{
        Success = $true
        Status  = 200
        Error   = ""
        Models  = [string[]]$ids
    }
}

$probeScript = {
    param(
        [string]$ApiBase,
        [string]$ApiKey,
        [string]$Model,
        [int]$FastTimeout,
        [int]$SlowTimeout
    )

    $ErrorActionPreference = "Stop"

    function Invoke-Request {
        param(
            [string]$Url,
            [hashtable]$Body,
            [int]$TimeoutSec
        )

        $handler = $null
        $client = $null

        try {
            $handler = New-Object System.Net.Http.HttpClientHandler
            $client = New-Object System.Net.Http.HttpClient($handler)
            $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSec)

            $request = New-Object System.Net.Http.HttpRequestMessage
            $request.Method = [System.Net.Http.HttpMethod]::Post
            $request.RequestUri = $Url
            $request.Headers.UserAgent.ParseAdd("9Router-Quick-Scanner/2.0")
            $request.Headers.Authorization =
                New-Object System.Net.Http.Headers.AuthenticationHeaderValue(
                    "Bearer",
                    $ApiKey
                )

            $json = $Body | ConvertTo-Json -Depth 20 -Compress

            $request.Content = New-Object System.Net.Http.StringContent(
                $json,
                [System.Text.Encoding]::UTF8,
                "application/json"
            )

            $started = Get-Date
            $response = $client.SendAsync($request).GetAwaiter().GetResult()
            $elapsed = ((Get-Date) - $started).TotalSeconds
            $content = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()

            return [PSCustomObject]@{
                StatusCode = [int]$response.StatusCode
                Seconds    = [math]::Round($elapsed, 2)
                Content    = $content
            }
        }
        catch {
            $elapsed = 0

            try {
                if ($null -ne $started) {
                    $elapsed = ((Get-Date) - $started).TotalSeconds
                }
            }
            catch {
                $elapsed = 0
            }

            return [PSCustomObject]@{
                StatusCode = 0
                Seconds    = [math]::Round($elapsed, 2)
                Content    = $_.Exception.Message
            }
        }
        finally {
            if ($null -ne $client) {
                $client.Dispose()
            }

            if ($null -ne $handler) {
                $handler.Dispose()
            }
        }
    }

    function Invoke-Protocol {
        param(
            [string]$Protocol,
            [int]$TimeoutSec
        )

        if ($Protocol -eq "responses") {
            $body = @{
                model = $Model
                input = "Reply with OK"
                max_output_tokens = 4
            }

            return Invoke-Request `
                -Url "$ApiBase/responses" `
                -Body $body `
                -TimeoutSec $TimeoutSec
        }

        $body = @{
            model = $Model
            messages = @(
                @{
                    role = "user"
                    content = "Reply with OK"
                }
            )
            max_tokens = 4
            stream = $false
        }

        $result = Invoke-Request `
            -Url "$ApiBase/chat/completions" `
            -Body $body `
            -TimeoutSec $TimeoutSec

        if (
            $result.StatusCode -eq 400 -and
            $result.Content -match "max_tokens|max.?completion"
        ) {
            $body.Remove("max_tokens")
            $body["max_completion_tokens"] = 4

            $result = Invoke-Request `
                -Url "$ApiBase/chat/completions" `
                -Body $body `
                -TimeoutSec $TimeoutSec
        }

        return $result
    }

    function New-LiveResult {
        param(
            [string]$Protocol,
            [object]$Result
        )

        return [PSCustomObject]@{
            Live     = $true
            Model    = $Model
            Protocol = $Protocol
            Seconds  = $Result.Seconds
        }
    }

    $id = $Model.ToLowerInvariant()

    if (
        $id -match "^gpt-5" -or
        $id.Contains("codex") -or
        $id.Contains("computer-use")
    ) {
        $firstProtocol = "responses"
        $secondProtocol = "chat"
    }
    else {
        $firstProtocol = "chat"
        $secondProtocol = "responses"
    }

    $first = Invoke-Protocol `
        -Protocol $firstProtocol `
        -TimeoutSec $FastTimeout

    if ($first.StatusCode -eq 200) {
        New-LiveResult -Protocol $firstProtocol -Result $first
        return
    }

    # A timeout may simply be a slow but usable upstream.
    if ($first.StatusCode -eq 0 -or $first.StatusCode -eq 504) {
        $slowFirst = Invoke-Protocol `
            -Protocol $firstProtocol `
            -TimeoutSec $SlowTimeout

        if ($slowFirst.StatusCode -eq 200) {
            New-LiveResult -Protocol $firstProtocol -Result $slowFirst
            return
        }

        [PSCustomObject]@{
            Live     = $false
            Model    = $Model
            Protocol = ""
            Seconds  = 0
        }

        return
    }

    # Clear route/shape mismatch: try the alternate OpenAI protocol once.
    if (
        $first.StatusCode -eq 400 -or
        $first.StatusCode -eq 404 -or
        $first.StatusCode -eq 405 -or
        $first.StatusCode -eq 422
    ) {
        $second = Invoke-Protocol `
            -Protocol $secondProtocol `
            -TimeoutSec $FastTimeout

        if ($second.StatusCode -eq 200) {
            New-LiveResult -Protocol $secondProtocol -Result $second
            return
        }

        if ($second.StatusCode -eq 0 -or $second.StatusCode -eq 504) {
            $slowSecond = Invoke-Protocol `
                -Protocol $secondProtocol `
                -TimeoutSec $SlowTimeout

            if ($slowSecond.StatusCode -eq 200) {
                New-LiveResult -Protocol $secondProtocol -Result $slowSecond
                return
            }
        }
    }

    [PSCustomObject]@{
        Live     = $false
        Model    = $Model
        Protocol = ""
        Seconds  = 0
    }
}

$xaml = @"
<Window
    xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    Title="9Router Quick Live Scanner"
    Width="640"
    Height="480"
    MinWidth="640"
    MinHeight="480"
    MaxWidth="640"
    MaxHeight="480"
    WindowStyle="None"
    ResizeMode="NoResize"
    Background="#1A1810"
    Foreground="#D4C89A"
    FontFamily="Verdana"
    FontSize="12"
    SnapsToDevicePixels="True"
    UseLayoutRounding="True"
    TextOptions.TextRenderingMode="Aliased"
    TextOptions.TextFormattingMode="Display">

    <Window.Resources>
        <SolidColorBrush x:Key="BackgroundBrush" Color="#1A1810"/>
        <SolidColorBrush x:Key="BackgroundSoftBrush" Color="#232018"/>
        <SolidColorBrush x:Key="SurfaceBrush" Color="#332E22"/>
        <SolidColorBrush x:Key="SurfaceRaisedBrush" Color="#3D372A"/>
        <SolidColorBrush x:Key="SurfaceAltBrush" Color="#453D30"/>
        <SolidColorBrush x:Key="BorderDarkBrush" Color="#100E08"/>
        <SolidColorBrush x:Key="BorderHighlightBrush" Color="#F0D060"/>
        <SolidColorBrush x:Key="BevelLightBrush" Color="#75663D"/>
        <SolidColorBrush x:Key="BorderMutedBrush" Color="#5A5040"/>
        <SolidColorBrush x:Key="TextPrimaryBrush" Color="#D4C89A"/>
        <SolidColorBrush x:Key="TextSecondaryBrush" Color="#9C9371"/>
        <SolidColorBrush x:Key="TextMutedBrush" Color="#6E674E"/>
        <SolidColorBrush x:Key="AccentTealBrush" Color="#008080"/>
        <SolidColorBrush x:Key="AccentTealDeepBrush" Color="#004C4C"/>
        <SolidColorBrush x:Key="SuccessBrush" Color="#4A7A20"/>
        <SolidColorBrush x:Key="WarningBrush" Color="#7A7A20"/>
        <SolidColorBrush x:Key="DangerBrush" Color="#7A2020"/>
        <SolidColorBrush x:Key="DangerTextBrush" Color="#D66464"/>
        <SolidColorBrush x:Key="SelectionBrush" Color="#3D372A"/>
        <SolidColorBrush x:Key="CompareBackBrush" Color="#14120C"/>
        <SolidColorBrush x:Key="LinkBrush" Color="#F0D060"/>

        <Style x:Key="LabelStyle" TargetType="TextBlock">
            <Setter Property="Foreground" Value="{StaticResource TextSecondaryBrush}"/>
            <Setter Property="FontSize" Value="11"/>
            <Setter Property="VerticalAlignment" Value="Center"/>
        </Style>

        <Style x:Key="ButtonStyle" TargetType="Button">
            <Setter Property="Foreground" Value="{StaticResource TextPrimaryBrush}"/>
            <Setter Property="Background" Value="{StaticResource SurfaceRaisedBrush}"/>
            <Setter Property="FontFamily" Value="Verdana"/>
            <Setter Property="FontSize" Value="11"/>
            <Setter Property="MinHeight" Value="24"/>
            <Setter Property="Padding" Value="6,2"/>
            <Setter Property="FocusVisualStyle" Value="{x:Null}"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Grid x:Name="Root" Background="{TemplateBinding Background}" SnapsToDevicePixels="True">
                            <Border x:Name="TopEdge" BorderBrush="{StaticResource BevelLightBrush}" BorderThickness="0,2,0,0"/>
                            <Border x:Name="LeftEdge" BorderBrush="{StaticResource BevelLightBrush}" BorderThickness="2,0,0,0"/>
                            <Border x:Name="BottomEdge" BorderBrush="{StaticResource BorderDarkBrush}" BorderThickness="0,0,0,2"/>
                            <Border x:Name="RightEdge" BorderBrush="{StaticResource BorderDarkBrush}" BorderThickness="0,0,2,0"/>
                            <ContentPresenter
                                x:Name="Content"
                                HorizontalAlignment="Center"
                                VerticalAlignment="Center"
                                Margin="5,2,5,2"/>
                        </Grid>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="Root" Property="Background" Value="{StaticResource SurfaceAltBrush}"/>
                            </Trigger>
                            <Trigger Property="IsPressed" Value="True">
                                <Setter TargetName="TopEdge" Property="BorderBrush" Value="{StaticResource BorderDarkBrush}"/>
                                <Setter TargetName="LeftEdge" Property="BorderBrush" Value="{StaticResource BorderDarkBrush}"/>
                                <Setter TargetName="BottomEdge" Property="BorderBrush" Value="{StaticResource BevelLightBrush}"/>
                                <Setter TargetName="RightEdge" Property="BorderBrush" Value="{StaticResource BevelLightBrush}"/>
                                <Setter TargetName="Root" Property="Background" Value="{StaticResource SurfaceBrush}"/>
                                <Setter TargetName="Content" Property="Margin" Value="6,3,4,1"/>
                            </Trigger>
                            <Trigger Property="IsKeyboardFocused" Value="True">
                                <Setter Property="Foreground" Value="{StaticResource LinkBrush}"/>
                            </Trigger>
                            <Trigger Property="IsEnabled" Value="False">
                                <Setter Property="Foreground" Value="{StaticResource TextMutedBrush}"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="PrimaryButtonStyle" TargetType="Button" BasedOn="{StaticResource ButtonStyle}">
            <Setter Property="Background" Value="{StaticResource AccentTealDeepBrush}"/>
            <Setter Property="FontSize" Value="12"/>
        </Style>

        <Style x:Key="TextBoxStyle" TargetType="TextBox">
            <Setter Property="Foreground" Value="{StaticResource TextPrimaryBrush}"/>
            <Setter Property="Background" Value="{StaticResource CompareBackBrush}"/>
            <Setter Property="CaretBrush" Value="{StaticResource LinkBrush}"/>
            <Setter Property="SelectionBrush" Value="{StaticResource SelectionBrush}"/>
            <Setter Property="FontFamily" Value="Verdana"/>
            <Setter Property="FontSize" Value="12"/>
            <Setter Property="Height" Value="24"/>
            <Setter Property="FocusVisualStyle" Value="{x:Null}"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="TextBox">
                        <Grid x:Name="Root" Background="{TemplateBinding Background}" SnapsToDevicePixels="True">
                            <Border x:Name="TopEdge" BorderBrush="{StaticResource BorderDarkBrush}" BorderThickness="0,2,0,0"/>
                            <Border x:Name="LeftEdge" BorderBrush="{StaticResource BorderDarkBrush}" BorderThickness="2,0,0,0"/>
                            <Border x:Name="BottomEdge" BorderBrush="{StaticResource BevelLightBrush}" BorderThickness="0,0,0,2"/>
                            <Border x:Name="RightEdge" BorderBrush="{StaticResource BevelLightBrush}" BorderThickness="0,0,2,0"/>
                            <ScrollViewer x:Name="PART_ContentHost" Margin="4,2,4,2"/>
                        </Grid>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsKeyboardFocused" Value="True">
                                <Setter TargetName="TopEdge" Property="BorderBrush" Value="{StaticResource BorderHighlightBrush}"/>
                                <Setter TargetName="LeftEdge" Property="BorderBrush" Value="{StaticResource BorderHighlightBrush}"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="PasswordBoxStyle" TargetType="PasswordBox">
            <Setter Property="Foreground" Value="{StaticResource TextPrimaryBrush}"/>
            <Setter Property="Background" Value="{StaticResource CompareBackBrush}"/>
            <Setter Property="CaretBrush" Value="{StaticResource LinkBrush}"/>
            <Setter Property="FontFamily" Value="Verdana"/>
            <Setter Property="FontSize" Value="12"/>
            <Setter Property="Height" Value="24"/>
            <Setter Property="FocusVisualStyle" Value="{x:Null}"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="PasswordBox">
                        <Grid x:Name="Root" Background="{TemplateBinding Background}" SnapsToDevicePixels="True">
                            <Border x:Name="TopEdge" BorderBrush="{StaticResource BorderDarkBrush}" BorderThickness="0,2,0,0"/>
                            <Border x:Name="LeftEdge" BorderBrush="{StaticResource BorderDarkBrush}" BorderThickness="2,0,0,0"/>
                            <Border x:Name="BottomEdge" BorderBrush="{StaticResource BevelLightBrush}" BorderThickness="0,0,0,2"/>
                            <Border x:Name="RightEdge" BorderBrush="{StaticResource BevelLightBrush}" BorderThickness="0,0,2,0"/>
                            <ScrollViewer x:Name="PART_ContentHost" Margin="4,2,4,2"/>
                        </Grid>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsKeyboardFocused" Value="True">
                                <Setter TargetName="TopEdge" Property="BorderBrush" Value="{StaticResource BorderHighlightBrush}"/>
                                <Setter TargetName="LeftEdge" Property="BorderBrush" Value="{StaticResource BorderHighlightBrush}"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="ResultItemStyle" TargetType="ListBoxItem">
            <Setter Property="Foreground" Value="{StaticResource TextPrimaryBrush}"/>
            <Setter Property="Background" Value="{StaticResource CompareBackBrush}"/>
            <Setter Property="HorizontalContentAlignment" Value="Stretch"/>
            <Setter Property="Padding" Value="0"/>
            <Setter Property="Margin" Value="0"/>
            <Setter Property="Height" Value="18"/>
            <Setter Property="FocusVisualStyle" Value="{x:Null}"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="ListBoxItem">
                        <Border
                            x:Name="Row"
                            Background="{TemplateBinding Background}"
                            BorderBrush="{StaticResource BorderMutedBrush}"
                            BorderThickness="0,0,0,1">
                            <ContentPresenter/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsSelected" Value="True">
                                <Setter TargetName="Row" Property="Background" Value="{StaticResource SelectionBrush}"/>
                                <Setter TargetName="Row" Property="BorderBrush" Value="{StaticResource BorderHighlightBrush}"/>
                            </Trigger>
                            <Trigger Property="IsKeyboardFocusWithin" Value="True">
                                <Setter TargetName="Row" Property="BorderBrush" Value="{StaticResource LinkBrush}"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <DataTemplate x:Key="ResultTemplate">
            <Grid Height="18">
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="390"/>
                    <ColumnDefinition Width="105"/>
                    <ColumnDefinition Width="80"/>
                </Grid.ColumnDefinitions>
                <TextBlock
                    Grid.Column="0"
                    Text="{Binding Model}"
                    Foreground="{StaticResource TextPrimaryBrush}"
                    FontSize="11"
                    VerticalAlignment="Center"
                    Margin="4,0,3,0"
                    TextTrimming="CharacterEllipsis"/>
                <TextBlock
                    Grid.Column="1"
                    Text="{Binding Protocol}"
                    Foreground="{StaticResource TextSecondaryBrush}"
                    FontSize="10"
                    VerticalAlignment="Center"
                    Margin="4,0,3,0"/>
                <TextBlock
                    Grid.Column="2"
                    Text="{Binding Time}"
                    Foreground="{StaticResource TextSecondaryBrush}"
                    FontSize="10"
                    VerticalAlignment="Center"
                    TextAlignment="Right"
                    Margin="3,0,5,0"/>
            </Grid>
        </DataTemplate>
    </Window.Resources>

    <Border BorderBrush="{StaticResource BorderDarkBrush}" BorderThickness="2" Background="{StaticResource BackgroundBrush}">
        <Grid>
            <Grid.RowDefinitions>
                <RowDefinition Height="20"/>
                <RowDefinition Height="*"/>
            </Grid.RowDefinitions>

            <Grid x:Name="TitleBar" Grid.Row="0" Background="{StaticResource SurfaceBrush}">
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="*"/>
                    <ColumnDefinition Width="22"/>
                    <ColumnDefinition Width="22"/>
                </Grid.ColumnDefinitions>

                <TextBlock
                    Grid.Column="0"
                    Text="9ROUTER QUICK LIVE SCANNER"
                    Foreground="{StaticResource TextPrimaryBrush}"
                    FontSize="16"
                    VerticalAlignment="Center"
                    Margin="5,-1,0,0"/>

                <Button
                    x:Name="MinimizeButton"
                    Grid.Column="1"
                    Content="_"
                    Style="{StaticResource ButtonStyle}"
                    MinHeight="20"
                    Height="20"
                    Padding="0"
                    FontSize="11"/>

                <Button
                    x:Name="CloseButton"
                    Grid.Column="2"
                    Content="X"
                    Style="{StaticResource ButtonStyle}"
                    MinHeight="20"
                    Height="20"
                    Padding="0"
                    FontSize="11"/>
            </Grid>

            <Grid Grid.Row="1" Margin="12,10,12,12">
                <Grid.RowDefinitions>
                    <RowDefinition Height="16"/>
                    <RowDefinition Height="26"/>
                    <RowDefinition Height="6"/>
                    <RowDefinition Height="16"/>
                    <RowDefinition Height="26"/>
                    <RowDefinition Height="10"/>
                    <RowDefinition Height="30"/>
                    <RowDefinition Height="8"/>
                    <RowDefinition Height="20"/>
                    <RowDefinition Height="20"/>
                    <RowDefinition Height="8"/>
                    <RowDefinition Height="20"/>
                    <RowDefinition Height="*"/>
                    <RowDefinition Height="30"/>
                </Grid.RowDefinitions>

                <TextBlock
                    Grid.Row="0"
                    Text="Provider"
                    Style="{StaticResource LabelStyle}"/>

                <TextBox
                    x:Name="ProviderBox"
                    Grid.Row="1"
                    Style="{StaticResource TextBoxStyle}"
                    Text="https://vsllm.com"/>

                <TextBlock
                    Grid.Row="3"
                    Text="API Key"
                    Style="{StaticResource LabelStyle}"/>

                <PasswordBox
                    x:Name="KeyBox"
                    Grid.Row="4"
                    Style="{StaticResource PasswordBoxStyle}"/>

                <Grid Grid.Row="6">
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="160"/>
                        <ColumnDefinition Width="6"/>
                        <ColumnDefinition Width="100"/>
                        <ColumnDefinition Width="6"/>
                        <ColumnDefinition Width="110"/>
                        <ColumnDefinition Width="6"/>
                        <ColumnDefinition Width="110"/>
                        <ColumnDefinition Width="*"/>
                    </Grid.ColumnDefinitions>

                    <Button
                        x:Name="ScanButton"
                        Grid.Column="0"
                        Content="SCAN MODELS"
                        Style="{StaticResource PrimaryButtonStyle}"/>

                    <Button
                        x:Name="StopButton"
                        Grid.Column="2"
                        Content="STOP SCAN"
                        Style="{StaticResource ButtonStyle}"/>

                    <Button
                        x:Name="CopyButton"
                        Grid.Column="4"
                        Content="COPY MODELS"
                        Style="{StaticResource ButtonStyle}"/>

                    <Button
                        x:Name="ClearButton"
                        Grid.Column="6"
                        Content="CLEAR LIST"
                        Style="{StaticResource ButtonStyle}"/>
                </Grid>

                <TextBlock
                    Grid.Row="8"
                    Text="Quick pass 4s  |  delayed timeout retry 12s  |  8 parallel"
                    Foreground="{StaticResource TextMutedBrush}"
                    FontSize="10"
                    VerticalAlignment="Center"/>

                <TextBlock
                    x:Name="StatusText"
                    Grid.Row="9"
                    Text="IDLE  |  Enter provider and API key, then scan."
                    Foreground="{StaticResource TextSecondaryBrush}"
                    FontSize="11"
                    VerticalAlignment="Center"
                    TextTrimming="CharacterEllipsis"/>

                <Grid Grid.Row="11" Background="{StaticResource SurfaceRaisedBrush}">
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="390"/>
                        <ColumnDefinition Width="105"/>
                        <ColumnDefinition Width="80"/>
                    </Grid.ColumnDefinitions>

                    <TextBlock
                        Grid.Column="0"
                        Text="MODEL"
                        Foreground="{StaticResource TextPrimaryBrush}"
                        FontSize="11"
                        VerticalAlignment="Center"
                        Margin="4,0,3,0"/>

                    <TextBlock
                        Grid.Column="1"
                        Text="API"
                        Foreground="{StaticResource TextPrimaryBrush}"
                        FontSize="11"
                        VerticalAlignment="Center"
                        Margin="4,0,3,0"/>

                    <TextBlock
                        Grid.Column="2"
                        Text="TIME"
                        Foreground="{StaticResource TextPrimaryBrush}"
                        FontSize="11"
                        VerticalAlignment="Center"
                        TextAlignment="Right"
                        Margin="3,0,5,0"/>
                </Grid>

                <Grid Grid.Row="12" Background="{StaticResource CompareBackBrush}">
                    <Border BorderBrush="{StaticResource BorderDarkBrush}" BorderThickness="0,2,0,0"/>
                    <Border BorderBrush="{StaticResource BorderDarkBrush}" BorderThickness="2,0,0,0"/>
                    <Border BorderBrush="{StaticResource BevelLightBrush}" BorderThickness="0,0,0,2"/>
                    <Border BorderBrush="{StaticResource BevelLightBrush}" BorderThickness="0,0,2,0"/>

                    <ListBox
                        x:Name="ResultList"
                        Margin="2"
                        Background="{StaticResource CompareBackBrush}"
                        Foreground="{StaticResource TextPrimaryBrush}"
                        BorderThickness="0"
                        ItemContainerStyle="{StaticResource ResultItemStyle}"
                        ItemTemplate="{StaticResource ResultTemplate}"
                        ScrollViewer.HorizontalScrollBarVisibility="Disabled"
                        ScrollViewer.VerticalScrollBarVisibility="Auto"/>
                </Grid>

                <Grid Grid.Row="13" Margin="0,6,0,0">
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="160"/>
                    </Grid.ColumnDefinitions>

                    <TextBlock
                        x:Name="BaseText"
                        Grid.Column="0"
                        Text="API BASE: -"
                        Foreground="{StaticResource TextMutedBrush}"
                        FontSize="10"
                        VerticalAlignment="Center"
                        TextTrimming="CharacterEllipsis"/>

                    <TextBlock
                        x:Name="CounterText"
                        Grid.Column="1"
                        Text="CHECKED 0/0  |  LIVE 0"
                        Foreground="{StaticResource TextPrimaryBrush}"
                        FontSize="10"
                        VerticalAlignment="Center"
                        TextAlignment="Right"/>
                </Grid>
            </Grid>
        </Grid>
    </Border>
</Window>
"@

$window = [Windows.Markup.XamlReader]::Parse($xaml)

$ProviderBox = $window.FindName("ProviderBox")
$KeyBox = $window.FindName("KeyBox")
$ScanButton = $window.FindName("ScanButton")
$StopButton = $window.FindName("StopButton")
$CopyButton = $window.FindName("CopyButton")
$ClearButton = $window.FindName("ClearButton")
$StatusText = $window.FindName("StatusText")
$BaseText = $window.FindName("BaseText")
$CounterText = $window.FindName("CounterText")
$ResultList = $window.FindName("ResultList")
$TitleBar = $window.FindName("TitleBar")
$MinimizeButton = $window.FindName("MinimizeButton")
$CloseButton = $window.FindName("CloseButton")

$script:CatalogPowerShell = $null
$script:CatalogHandle = $null
$script:RunspacePool = $null
$script:Tasks = New-Object System.Collections.ArrayList
$script:Scanning = $false
$script:ScanApiBase = ""
$script:ScanApiKey = ""
$script:TotalModels = 0
$script:CheckedModels = 0
$script:LiveModels = 0

$timer = New-Object System.Windows.Threading.DispatcherTimer
$timer.Interval = [TimeSpan]::FromMilliseconds(120)

function Set-Status {
    param(
        [string]$Text,
        [string]$Kind = "normal"
    )

    $StatusText.Text = $Text

    switch ($Kind) {
        "error" {
            $StatusText.Foreground = $window.Resources["DangerTextBrush"]
        }
        "success" {
            $StatusText.Foreground = $window.Resources["TextPrimaryBrush"]
        }
        default {
            $StatusText.Foreground = $window.Resources["TextSecondaryBrush"]
        }
    }
}

function Update-Counter {
    $CounterText.Text =
        "CHECKED $script:CheckedModels/$script:TotalModels  |  LIVE $script:LiveModels"
}

function Dispose-CatalogTask {
    if ($null -ne $script:CatalogPowerShell) {
        try {
            if ($null -ne $script:CatalogHandle -and -not $script:CatalogHandle.IsCompleted) {
                $script:CatalogPowerShell.Stop()
            }
        }
        catch {
        }

        try {
            $script:CatalogPowerShell.Dispose()
        }
        catch {
        }

        $script:CatalogPowerShell = $null
        $script:CatalogHandle = $null
    }
}

function Dispose-ProbeTasks {
    foreach ($task in @($script:Tasks)) {
        try {
            if (-not $task.Handle.IsCompleted) {
                $task.PowerShell.Stop()
            }
        }
        catch {
        }

        try {
            $task.PowerShell.Dispose()
        }
        catch {
        }
    }

    $script:Tasks.Clear()

    if ($null -ne $script:RunspacePool) {
        try {
            $script:RunspacePool.Close()
        }
        catch {
        }

        try {
            $script:RunspacePool.Dispose()
        }
        catch {
        }

        $script:RunspacePool = $null
    }
}

function Stop-CurrentScan {
    param([bool]$UserRequested = $false)

    if (-not $script:Scanning) {
        if ($UserRequested) {
            Set-Status "IDLE  |  Nothing is running."
        }

        return
    }

    Dispose-CatalogTask
    Dispose-ProbeTasks

    $script:Scanning = $false
    $script:ScanApiKey = ""

    if ($UserRequested) {
        Set-Status "STOPPED  |  Checked $script:CheckedModels/$script:TotalModels. Live $script:LiveModels."
    }
}

function Start-ProbePool {
    param([string[]]$Models)

    $script:TotalModels = $Models.Count
    $script:CheckedModels = 0
    $script:LiveModels = 0
    Update-Counter

    if ($script:TotalModels -eq 0) {
        $script:Scanning = $false
        Set-Status "DONE  |  No likely text models were returned." "error"
        return
    }

    $script:RunspacePool =
        [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspacePool(
            1,
            $MaxConcurrency
        )

    $script:RunspacePool.Open()

    $probeText = $probeScript.ToString()

    foreach ($model in $Models) {
        $ps = [System.Management.Automation.PowerShell]::Create()
        $ps.RunspacePool = $script:RunspacePool

        $null = $ps.AddScript($probeText)
        $null = $ps.AddArgument($script:ScanApiBase)
        $null = $ps.AddArgument($script:ScanApiKey)
        $null = $ps.AddArgument([string]$model)
        $null = $ps.AddArgument($FastTimeoutSec)
        $null = $ps.AddArgument($SlowTimeoutSec)

        $handle = $ps.BeginInvoke()

        $null = $script:Tasks.Add(
            [PSCustomObject]@{
                Model      = [string]$model
                PowerShell = $ps
                Handle     = $handle
            }
        )
    }

    Set-Status "SCANNING  |  0/$script:TotalModels checked. Live 0."
}

function Start-CatalogScan {
    if ($script:Scanning) {
        Set-Status "SCANNING  |  A scan is already running. Use STOP SCAN first."
        return
    }

    try {
        $apiBase = Resolve-ApiBase -InputValue $ProviderBox.Text
    }
    catch {
        Set-Status ("INPUT ERROR  |  " + $_.Exception.Message) "error"
        $ProviderBox.Focus()
        return
    }

    $apiKey = $KeyBox.Password.Trim()

    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        Set-Status "INPUT ERROR  |  API key is empty." "error"
        $KeyBox.Focus()
        return
    }

    $ResultList.Items.Clear()

    $script:ScanApiBase = $apiBase
    $script:ScanApiKey = $apiKey
    $script:TotalModels = 0
    $script:CheckedModels = 0
    $script:LiveModels = 0
    Update-Counter

    $BaseText.Text = "API BASE: $script:ScanApiBase"
    Set-Status "READING CATALOG  |  Requesting /v1/models..."

    $script:Scanning = $true

    $script:CatalogPowerShell = [System.Management.Automation.PowerShell]::Create()
    $null = $script:CatalogPowerShell.AddScript($catalogScript.ToString())
    $null = $script:CatalogPowerShell.AddArgument($script:ScanApiBase)
    $null = $script:CatalogPowerShell.AddArgument($script:ScanApiKey)
    $script:CatalogHandle = $script:CatalogPowerShell.BeginInvoke()

    if (-not $timer.IsEnabled) {
        $timer.Start()
    }
}

function Process-CatalogCompletion {
    if (
        $null -eq $script:CatalogPowerShell -or
        $null -eq $script:CatalogHandle -or
        -not $script:CatalogHandle.IsCompleted
    ) {
        return
    }

    try {
        $items = @($script:CatalogPowerShell.EndInvoke($script:CatalogHandle))
    }
    catch {
        $message = $_.Exception.Message
        Dispose-CatalogTask
        $script:Scanning = $false
        $script:ScanApiKey = ""
        Set-Status ("CATALOG ERROR  |  " + $message) "error"
        return
    }

    Dispose-CatalogTask

    if ($items.Count -eq 0) {
        $script:Scanning = $false
        $script:ScanApiKey = ""
        Set-Status "CATALOG ERROR  |  Provider returned no catalog result." "error"
        return
    }

    $catalog = $items[0]

    if (-not [bool]$catalog.Success) {
        $script:Scanning = $false
        $script:ScanApiKey = ""

        $statusCode = [int]$catalog.Status
        $detail = [string]$catalog.Error

        Set-Status (
            "CATALOG ERROR  |  HTTP $statusCode  |  $detail"
        ) "error"

        return
    }

    $models = @($catalog.Models)

    if ($models.Count -eq 0) {
        $script:Scanning = $false
        $script:ScanApiKey = ""
        Set-Status "DONE  |  Catalog contains no likely text models." "error"
        return
    }

    Start-ProbePool -Models ([string[]]$models)
}

function Process-ProbeCompletions {
    if ($script:Tasks.Count -eq 0) {
        return
    }

    $completed = New-Object System.Collections.ArrayList

    foreach ($task in @($script:Tasks)) {
        if (-not $task.Handle.IsCompleted) {
            continue
        }

        $result = $null

        try {
            $items = @($task.PowerShell.EndInvoke($task.Handle))

            if ($items.Count -gt 0) {
                $result = $items[0]
            }
        }
        catch {
            $result = $null
        }

        try {
            $task.PowerShell.Dispose()
        }
        catch {
        }

        $null = $completed.Add($task)
        $script:CheckedModels++

        if ($null -ne $result -and [bool]$result.Live) {
            $script:LiveModels++

            $protocolText = ([string]$result.Protocol).ToUpperInvariant()

            $row = [PSCustomObject]@{
                Model    = [string]$result.Model
                Protocol = $protocolText
                Time     = ("{0:N2}s" -f [double]$result.Seconds)
            }

            $null = $ResultList.Items.Add($row)
        }
    }

    foreach ($task in @($completed)) {
        $null = $script:Tasks.Remove($task)
    }

    Update-Counter

    if ($script:Scanning) {
        Set-Status (
            "SCANNING  |  $script:CheckedModels/$script:TotalModels checked. Live $script:LiveModels."
        )
    }

    if (
        $script:Scanning -and
        $script:TotalModels -gt 0 -and
        $script:CheckedModels -ge $script:TotalModels -and
        $script:Tasks.Count -eq 0
    ) {
        if ($null -ne $script:RunspacePool) {
            try {
                $script:RunspacePool.Close()
            }
            catch {
            }

            try {
                $script:RunspacePool.Dispose()
            }
            catch {
            }

            $script:RunspacePool = $null
        }

        $script:Scanning = $false
        $script:ScanApiKey = ""

        Set-Status (
            "DONE  |  Checked $script:CheckedModels models. Live $script:LiveModels."
        ) "success"
    }
}

$timer.Add_Tick({
    if (-not $script:Scanning) {
        return
    }

    if ($null -ne $script:CatalogPowerShell) {
        Process-CatalogCompletion
        return
    }

    Process-ProbeCompletions
})

$ScanButton.Add_Click({
    Start-CatalogScan
})

$StopButton.Add_Click({
    Stop-CurrentScan -UserRequested $true
})

$CopyButton.Add_Click({
    if ($ResultList.Items.Count -eq 0) {
        Set-Status "COPY  |  No live models to copy."
        return
    }

    $lines = New-Object System.Collections.Generic.List[string]

    foreach ($item in $ResultList.Items) {
        $lines.Add([string]$item.Model)
    }

    [System.Windows.Clipboard]::SetText(
        ($lines -join [Environment]::NewLine)
    )

    Set-Status "COPY  |  Copied $($ResultList.Items.Count) live model IDs."
})

$ClearButton.Add_Click({
    if ($script:Scanning) {
        Set-Status "SCANNING  |  Stop the current scan before clearing results."
        return
    }

    $ResultList.Items.Clear()
    $script:TotalModels = 0
    $script:CheckedModels = 0
    $script:LiveModels = 0
    Update-Counter
    Set-Status "IDLE  |  Result list cleared."
})

$ProviderBox.Add_KeyDown({
    param($sender, $eventArgs)

    if ($eventArgs.Key -eq [System.Windows.Input.Key]::Enter) {
        $KeyBox.Focus()
        $eventArgs.Handled = $true
    }
})

$KeyBox.Add_KeyDown({
    param($sender, $eventArgs)

    if ($eventArgs.Key -eq [System.Windows.Input.Key]::Enter) {
        Start-CatalogScan
        $eventArgs.Handled = $true
    }
})

$TitleBar.Add_MouseLeftButtonDown({
    param($sender, $eventArgs)

    if ($eventArgs.ButtonState -eq [System.Windows.Input.MouseButtonState]::Pressed) {
        try {
            $window.DragMove()
        }
        catch {
        }
    }
})

$MinimizeButton.Add_Click({
    $window.WindowState = [System.Windows.WindowState]::Minimized
})

$CloseButton.Add_Click({
    Stop-CurrentScan
    $window.Close()
})

$window.Add_Closing({
    Stop-CurrentScan

    if ($timer.IsEnabled) {
        $timer.Stop()
    }
})

$null = $window.ShowDialog()
