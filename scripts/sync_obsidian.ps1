[CmdletBinding()]
param(
    [string]$VaultPath = "D:\TOOL\OBSIDIAN\Home\prompt仓库",
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$destinationRoot = Join-Path $VaultPath "DPMS-Platform"
$legacyRoot = Join-Path $VaultPath "DPMS"

$directories = @(
    "00_导航",
    "01_项目基线",
    "01_项目基线\历史恢复资料",
    "02_架构演进",
    "03_工作流与运行时",
    "04_Bilibili里程碑",
    "05_运维安全与前端",
    "06_审查与风险",
    "07_文档与治理",
    "99_附件"
)

$mappings = @(
    @{ Source = "README.md"; Destination = "01_项目基线\DPMS_项目说明.md" },
    @{ Source = "docs\DPMS_能力演化路线图_v4-v9_20260605.md"; Destination = "01_项目基线\DPMS_能力演化路线图_v4-v9_20260605.md" },
    @{ Source = "docs\DPMS_总设计方案_v1_20260611.md"; Destination = "01_项目基线\DPMS_总设计方案_v1_20260611.md" },
    @{ Source = "docs\DPMS_V8-V9_后续版本规划_20260612.md"; Destination = "01_项目基线\DPMS_V8-V9_后续版本规划_20260612.md" },
    @{ Source = "docs\DPMS_V10-V13_运营规模化_后续版本规划_20260614.md"; Destination = "01_项目基线\DPMS_V10-V13_运营规模化_后续版本规划_20260614.md" },
    @{ Source = "docs\DPMS_EventStore_V4_实施记录_20260605.md"; Destination = "02_架构演进\DPMS_EventStore_V4_实施记录_20260605.md" },
    @{ Source = "docs\DPMS_KnowledgeRuntime_实施记录_20260606.md"; Destination = "02_架构演进\DPMS_KnowledgeRuntime_实施记录_20260606.md" },
    @{ Source = "docs\DPMS_StrategyAdvice_实施记录_20260606.md"; Destination = "02_架构演进\DPMS_StrategyAdvice_实施记录_20260606.md" },
    @{ Source = "docs\DPMS_StrategyKnowledgeBridge_实施记录_20260606.md"; Destination = "02_架构演进\DPMS_StrategyKnowledgeBridge_实施记录_20260606.md" },
    @{ Source = "docs\DPMS_StrategyQueue_实施记录_20260606.md"; Destination = "02_架构演进\DPMS_StrategyQueue_实施记录_20260606.md" },
    @{ Source = "docs\DPMS_StrategyEngineExtraction_实施记录_20260611.md"; Destination = "02_架构演进\DPMS_StrategyEngineExtraction_实施记录_20260611.md" },
    @{ Source = "docs\DPMS_DecisionExplainability_实施记录_20260611.md"; Destination = "02_架构演进\DPMS_DecisionExplainability_实施记录_20260611.md" },
    @{ Source = "docs\DPMS_V5.5-V7_RuntimeExpansion_实施记录_20260612.md"; Destination = "02_架构演进\DPMS_V5.5-V7_RuntimeExpansion_实施记录_20260612.md" },
    @{ Source = "docs\DPMS_V8_TransitionRuntime_实施记录_20260613.md"; Destination = "02_架构演进\DPMS_V8_TransitionRuntime_实施记录_20260613.md" },
    @{ Source = "docs\DPMS_V9_SemanticRuntime_实施记录_20260613.md"; Destination = "02_架构演进\DPMS_V9_SemanticRuntime_实施记录_20260613.md" },
    @{ Source = "docs\DPMS_V10-V13_OperationalScaling_实施记录_20260614.md"; Destination = "02_架构演进\DPMS_V10-V13_OperationalScaling_实施记录_20260614.md" },
    @{ Source = "docs\DPMS_ShadowRun_实施记录_20260605.md"; Destination = "03_工作流与运行时\DPMS_ShadowRun_实施记录_20260605.md" },
    @{ Source = "docs\DPMS_RuntimeRollback_实施记录_20260606.md"; Destination = "03_工作流与运行时\DPMS_RuntimeRollback_实施记录_20260606.md" },
    @{ Source = "docs\DPMS_WeiboXiaohongshuLotteryModule_实施记录_20260611.md"; Destination = "03_工作流与运行时\DPMS_WeiboXiaohongshuLotteryModule_实施记录_20260611.md" },
    @{ Source = "docs\DPMS_AllPlatformLotteryRules_实施记录_20260611.md"; Destination = "03_工作流与运行时\DPMS_AllPlatformLotteryRules_实施记录_20260611.md" },
    @{ Source = "docs\DPMS_DeployReadinessGeneralization_实施记录_20260611.md"; Destination = "05_运维安全与前端\DPMS_DeployReadinessGeneralization_实施记录_20260611.md" },
    @{ Source = "docs\DPMS_BilibiliGateNextAction_实施记录_20260608.md"; Destination = "04_Bilibili里程碑\DPMS_BilibiliGateNextAction_实施记录_20260608.md" },
    @{ Source = "docs\DPMS_BilibiliGateNotification_实施记录_20260608.md"; Destination = "04_Bilibili里程碑\DPMS_BilibiliGateNotification_实施记录_20260608.md" },
    @{ Source = "docs\DPMS_BilibiliGateUI_实施记录_20260608.md"; Destination = "04_Bilibili里程碑\DPMS_BilibiliGateUI_实施记录_20260608.md" },
    @{ Source = "docs\DPMS_BilibiliReadinessWizard_实施记录_20260608.md"; Destination = "04_Bilibili里程碑\DPMS_BilibiliReadinessWizard_实施记录_20260608.md" },
    @{ Source = "docs\DPMS_BilibiliSafetyGate_实施记录_20260608.md"; Destination = "04_Bilibili里程碑\DPMS_BilibiliSafetyGate_实施记录_20260608.md" },
    @{ Source = "docs\DPMS_BilibiliSafeWorkflow_实施记录_20260609.md"; Destination = "04_Bilibili里程碑\DPMS_BilibiliSafeWorkflow_实施记录_20260609.md" },
    @{ Source = "docs\DPMS_BilibiliTargetValidation_实施记录_20260609.md"; Destination = "04_Bilibili里程碑\DPMS_BilibiliTargetValidation_实施记录_20260609.md" },
    @{ Source = "docs\DPMS_BilibiliOfficialQrLogin_实施记录_20260609.md"; Destination = "04_Bilibili里程碑\DPMS_BilibiliOfficialQrLogin_实施记录_20260609.md" },
    @{ Source = "docs\DPMS_BilibiliDiscoveryRulePlan_实施记录_20260609.md"; Destination = "04_Bilibili里程碑\DPMS_BilibiliDiscoveryRulePlan_实施记录_20260609.md" },
    @{ Source = "docs\DPMS_BilibiliApiRealRun_实施记录_20260624.md"; Destination = "04_Bilibili里程碑\DPMS_BilibiliApiRealRun_实施记录_20260624.md" },
    @{ Source = "docs\DPMS_BilibiliDiscoveryExpansion_实施记录_20260624.md"; Destination = "04_Bilibili里程碑\DPMS_BilibiliDiscoveryExpansion_实施记录_20260624.md" },
    @{ Source = "docs\DPMS_BilibiliRulePlanSafety_实施记录_20260624.md"; Destination = "04_Bilibili里程碑\DPMS_BilibiliRulePlanSafety_实施记录_20260624.md" },
    @{ Source = "docs\DPMS_BilibiliMissingActionRepair_实施记录_20260624.md"; Destination = "04_Bilibili里程碑\DPMS_BilibiliMissingActionRepair_实施记录_20260624.md" },
    @{ Source = "docs\DPMS_FrontendTheme_实施记录_20260608.md"; Destination = "05_运维安全与前端\DPMS_FrontendTheme_实施记录_20260608.md" },
    @{ Source = "docs\DPMS_NotificationSecretBundle_实施记录_20260608.md"; Destination = "05_运维安全与前端\DPMS_NotificationSecretBundle_实施记录_20260608.md" },
    @{ Source = "docs\DPMS_最终搭建审阅与漏洞清单_20260602.md"; Destination = "06_审查与风险\DPMS_最终搭建审阅与漏洞清单_20260602.md" },
    @{ Source = "docs\DPMS_BilibiliGateReviewCleanup_实施记录_20260611.md"; Destination = "06_审查与风险\DPMS_BilibiliGateReviewCleanup_实施记录_20260611.md" },
    @{ Source = "docs\DPMS_运行时可信度硬化_实施记录_20260614.md"; Destination = "06_审查与风险\DPMS_运行时可信度硬化_实施记录_20260614.md" },
    @{ Source = "docs\DPMS_ObsidianKnowledgeBase_实施记录_20260609.md"; Destination = "07_文档与治理\DPMS_ObsidianKnowledgeBase_实施记录_20260609.md" },
    @{ Source = "docs\legacy_selectors_fragment.txt"; Destination = "99_附件\legacy_selectors_fragment.txt" },
    @{ Source = "obsidian\DPMS-Platform\00_导航\DPMS_项目主页.md"; Destination = "00_导航\DPMS_项目主页.md" },
    @{ Source = "obsidian\DPMS-Platform\00_导航\DPMS_活动时间线.md"; Destination = "00_导航\DPMS_活动时间线.md" },
    @{ Source = "obsidian\DPMS-Platform\00_导航\DPMS_记录规范.md"; Destination = "00_导航\DPMS_记录规范.md" }
)

$legacyMappings = @(
    "DPMS_全部功能整理_不删减.md",
    "DPMS_最终搭建审阅与漏洞清单.md",
    "源代码.md"
)

if (-not (Test-Path -LiteralPath $VaultPath)) {
    throw "Obsidian Vault does not exist: $VaultPath"
}

if (-not $VerifyOnly) {
    foreach ($directory in $directories) {
        New-Item -ItemType Directory -Path (Join-Path $destinationRoot $directory) -Force | Out-Null
    }
}

$checks = [System.Collections.Generic.List[object]]::new()

foreach ($mapping in $mappings) {
    $source = Join-Path $repoRoot $mapping.Source
    $destination = Join-Path $destinationRoot $mapping.Destination
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Source file missing: $source"
    }
    if (-not $VerifyOnly) {
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
    $checks.Add([pscustomobject]@{
        Source = $source
        Destination = $destination
    })
}

foreach ($name in $legacyMappings) {
    $source = Join-Path $legacyRoot $name
    $destination = Join-Path $destinationRoot ("01_项目基线\历史恢复资料\" + $name)
    if (-not (Test-Path -LiteralPath $source)) {
        Write-Warning "Legacy source file missing; skipped: $source"
        continue
    }
    if (-not $VerifyOnly) {
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
    $checks.Add([pscustomobject]@{
        Source = $source
        Destination = $destination
    })
}

$failures = [System.Collections.Generic.List[string]]::new()
foreach ($check in $checks) {
    if (-not (Test-Path -LiteralPath $check.Destination)) {
        $failures.Add("Missing destination: $($check.Destination)")
        continue
    }
    $sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $check.Source).Hash
    $destinationHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $check.Destination).Hash
    if ($sourceHash -ne $destinationHash) {
        $failures.Add("Hash mismatch: $($check.Destination)")
    }
}

$markdownFiles = Get-ChildItem -LiteralPath $destinationRoot -Recurse -File -Filter "*.md"
$noteNames = @{}
foreach ($file in $markdownFiles) {
    $name = [System.IO.Path]::GetFileNameWithoutExtension($file.Name)
    if (-not $noteNames.ContainsKey($name)) {
        $noteNames[$name] = 0
    }
    $noteNames[$name] += 1
}

$linkCount = 0
$navigationRoot = Join-Path $destinationRoot "00_导航"
foreach ($file in Get-ChildItem -LiteralPath $navigationRoot -File -Filter "*.md") {
    $content = Get-Content -Raw -Encoding UTF8 -LiteralPath $file.FullName
    foreach ($match in [regex]::Matches($content, "\[\[([^\]|#]+)")) {
        $target = $match.Groups[1].Value.Trim()
        $linkCount += 1
        if (-not $noteNames.ContainsKey($target)) {
            $failures.Add("Unresolved Wiki link '$target' in $($file.FullName)")
        }
        elseif ($noteNames[$target] -ne 1) {
            $failures.Add("Ambiguous Wiki link '$target' in $($file.FullName)")
        }
    }
}

if ($failures.Count -gt 0) {
    $failures | ForEach-Object { Write-Error $_ }
    throw "Obsidian synchronization verification failed with $($failures.Count) error(s)."
}

$mode = if ($VerifyOnly) { "verified" } else { "synchronized" }
Write-Output "DPMS-Platform Obsidian knowledge base $mode successfully."
Write-Output "Vault: $VaultPath"
Write-Output "Files: $($checks.Count)"
Write-Output "Wiki links: $linkCount"
