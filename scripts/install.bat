@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM shuohao-skills Windows installer
REM
REM 用法：
REM   install.bat
REM   install.bat novel-characters
REM   install.bat novel-storyboard
REM   install.bat --claude
REM   install.bat --codex
REM   install.bat --claude novel-storyboard
REM   install.bat --codex novel-storyboard
REM   install.bat --uninstall
REM ============================================================

REM 当前脚本位于：REPO\scripts\install.bat
REM 所以仓库根目录就是上一级
set "REPO=%~dp0.."
for %%I in ("%REPO%") do set "REPO=%%~fI"

set "SKILLS_DIR=%REPO%\skills"

set "TARGET_CLAUDE=%USERPROFILE%\.claude\skills"
set "TARGET_CODEX=%USERPROFILE%\.codex\skills"

set "USE_CLAUDE=0"
set "USE_CODEX=0"
set "UNINSTALL=0"

set "WANTED="

REM ============================================================
REM 参数解析
REM ============================================================

:parse_args

if "%~1"=="" goto after_args

if /I "%~1"=="--claude" (
    set "USE_CLAUDE=1"
    shift
    goto parse_args
)

if /I "%~1"=="--codex" (
    set "USE_CODEX=1"
    shift
    goto parse_args
)

if /I "%~1"=="--uninstall" (
    set "UNINSTALL=1"
    shift
    goto parse_args
)

if /I "%~1"=="-h" goto help
if /I "%~1"=="--help" goto help

if "%~1"=="-" (
    echo [ERROR] 未知选项: %~1
    exit /b 1
)

REM 普通参数 = skill 名称
if defined WANTED (
    set "WANTED=!WANTED!|%~1"
) else (
    set "WANTED=%~1"
)

shift
goto parse_args


:after_args

REM ============================================================
REM 自动检测 Claude / Codex
REM 如果用户没有指定 --claude / --codex
REM ============================================================

if "%USE_CLAUDE%"=="0" if "%USE_CODEX%"=="0" (

    if exist "%USERPROFILE%\.claude" (
        set "USE_CLAUDE=1"
    )

    if exist "%USERPROFILE%\.codex" (
        set "USE_CODEX=1"
    )
)

REM 如果都不存在，则默认创建 Claude 和 Codex 目录？
REM 为了和原始脚本行为一致，这里不自动创建 agent 根目录。
if "%USE_CLAUDE%"=="0" if "%USE_CODEX%"=="0" (
    echo [ERROR] 没找到：
    echo   %USERPROFILE%\.claude
    echo   %USERPROFILE%\.codex
    echo.
    echo 请先安装 Claude Code 或 Codex，
    echo 或者明确指定 --claude / --codex。
    exit /b 1
)

REM ============================================================
REM 检查 skills 目录
REM ============================================================

if not exist "%SKILLS_DIR%" (
    echo [ERROR] 找不到 skills 目录：
    echo %SKILLS_DIR%
    exit /b 1
)

REM ============================================================
REM 如果没有指定 skill，则安装全部
REM ============================================================

if not defined WANTED (
    for /D %%D in ("%SKILLS_DIR%\*") do (
        if exist "%%D\SKILL.md" (
            if defined WANTED (
                set "WANTED=!WANTED!|%%~nxD"
            ) else (
                set "WANTED=%%~nxD"
            )
        )
    )
)

if not defined WANTED (
    echo [ERROR] 没有找到任何有效 Skill。
    exit /b 1
)

REM ============================================================
REM 创建目标 skills 目录
REM ============================================================

if "%USE_CLAUDE%"=="1" (
    if not exist "%TARGET_CLAUDE%" mkdir "%TARGET_CLAUDE%"
)

if "%USE_CODEX%"=="1" (
    if not exist "%TARGET_CODEX%" mkdir "%TARGET_CODEX%"
)

REM ============================================================
REM 安装
REM ============================================================

if "%USE_CLAUDE%"=="1" (
    call :install_to "%TARGET_CLAUDE%"
    if errorlevel 1 exit /b 1
)

if "%USE_CODEX%"=="1" (
    call :install_to "%TARGET_CODEX%"
    if errorlevel 1 exit /b 1
)

echo.
echo ==========================================
if "%UNINSTALL%"=="1" (
    echo 卸载完成
) else (
    echo 安装完成
)
echo ==========================================
exit /b 0


REM ============================================================
REM install_to
REM ============================================================

:install_to

set "TARGET=%~1"

echo.
echo ------------------------------------------
echo 目标：
echo %TARGET%
echo ------------------------------------------

for %%N in (%WANTED:|= %) do (

    set "NAME=%%N"
    set "SRC=%SKILLS_DIR%\%%N"
    set "DST=%TARGET%\%%N"

    if not exist "!SRC!\SKILL.md" (
        echo [ERROR] %%N 不是有效 Skill，缺少：
        echo !SRC!\SKILL.md
        exit /b 1
    )

    if "%UNINSTALL%"=="1" (

        if exist "!DST!" (
            echo 删除：!DST!

            REM rmdir 可以删除 Junction，
            REM 不会删除源目录
            rmdir "!DST!" 2>nul

            if exist "!DST!" (
                echo [ERROR] 无法删除：
                echo !DST!
                exit /b 1
            )
        ) else (
            echo 不存在：!DST!
        )

    ) else (

        REM 如果目标已经存在
        if exist "!DST!" (

            REM Junction / 目录都可能被 exist 检测到。
            REM 为安全起见，不覆盖真实目录。
            echo [SKIP] 已存在：
            echo !DST!
            echo        请确认后手动删除，再重新运行。
            echo.

        ) else (

            echo 创建：
            echo   !DST!
            echo   ^<-- !SRC!

            mklink /J "!DST!" "!SRC!" >nul

            if errorlevel 1 (
                echo [ERROR] 创建 Junction 失败。
                exit /b 1
            )

            echo [OK] %%N
        )
    )
)

exit /b 0


:help

echo.
echo shuohao-skills Windows 安装脚本
echo.
echo 用法：
echo.
echo   install.bat
echo       自动检测 Claude Code / Codex，并安装全部 Skill
echo.
echo   install.bat novel-storyboard
echo       安装指定 Skill
echo.
echo   install.bat --claude
echo       全部 Skill 安装到 Claude Code
echo.
echo   install.bat --codex
echo       全部 Skill 安装到 Codex
echo.
echo   install.bat --claude novel-storyboard
echo       将 novel-storyboard 安装到 Claude Code
echo.
echo   install.bat --uninstall
echo       删除已建立的 Junction
echo.
exit /b 0