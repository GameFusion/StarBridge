#include "ProcessRunner.h"
#include <QDir>
#include <QDebug>

ProcessRunner::ProcessRunner(QObject *parent)
    : QObject(parent)
{
    connect(&process, &QProcess::readyReadStandardOutput, this, &ProcessRunner::onReadyReadStandardOutput);
    connect(&process, &QProcess::readyReadStandardError, this, &ProcessRunner::onReadyReadStandardError);
    connect(&process, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished),
            this, &ProcessRunner::onProcessFinished);
}

void ProcessRunner::startStarBridge()
{
    QStringList args;
    args << "app.py";

    emit logMessage("Working directory: " + QDir::currentPath(), false);

    QString pythonPath = QDir::currentPath() + "/venv/bin/python";
    QString appPath = QDir::currentPath() + "/app.py";

    // Verify files exist
    if (!QFile::exists(pythonPath)) {
        emit logMessage("ERROR: venv/bin/python not found!", true);
        return;
    }
    if (!QFile::exists(appPath)) {
        emit logMessage("ERROR: app.py not found!", true);
        return;
    }

    // Build proper environment with venv/bin in PATH
    QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
    // Add venv/bin to PATH (macOS/Linux + Windows)
    QString venvBin = QDir::currentPath() + "/venv/bin";
#ifdef Q_OS_WIN
    venvBin = basePath + "\\venv\\Scripts";  // Windows uses Scripts/
#endif
    QString currentPath = env.value("PATH");
    QString newPath = venvBin + QString(QDir::separator()) + ".." + QDir::separator() + currentPath;

    env.insert("PATH", venvBin + QDir::listSeparator() + currentPath);

    // Optional: Force Python to use correct venv
    env.insert("VIRTUAL_ENV", QDir::currentPath() + "/venv");
    env.insert("PYTHONPATH", QDir::currentPath());  // if needed

    process.setProcessEnvironment(env);

    process.setWorkingDirectory(QDir::currentPath());
    process.start("venv/bin/python", args);

    emit logMessage("Starting StarBridge...", false);

}

void ProcessRunner::handleOutput(const QString &output)
{
    const QStringList lines = output.split('\n', Qt::SkipEmptyParts);
    for (const QString &line : lines) {
        QString cleaned = line.trimmed();
        if (cleaned.isEmpty()) continue;

        bool isError = false;
        QString level;

        if (cleaned.contains("ERROR", Qt::CaseInsensitive) ||
            cleaned.contains("CRITICAL", Qt::CaseInsensitive) ||
            cleaned.contains("exception", Qt::CaseInsensitive)) {
            isError = true;
            level = "ERROR";
        }
        else if (cleaned.contains("WARNING", Qt::CaseInsensitive)) {
            level = "WARNING";
        }
        else if (cleaned.contains("INFO", Qt::CaseInsensitive)) {
            level = "INFO";
        }
        else if (cleaned.contains("DEBUG", Qt::CaseInsensitive)) {
            level = "DEBUG";
        }

        emit logMessage(cleaned, isError);
    }
}

void ProcessRunner::onReadyReadStandardOutput() {
    handleOutput(process.readAllStandardOutput());
}

void ProcessRunner::onReadyReadStandardError() {
    handleOutput(process.readAllStandardError());
}

/*
void ProcessRunner::onReadyReadStandardOutput()
{
    QString output = process.readAllStandardOutput();
    emit logMessage(output.trimmed(), false);
}

void ProcessRunner::onReadyReadStandardError()
{
    QString error = process.readAllStandardError();
    emit logMessage(error.trimmed(), true);
}
*/

void ProcessRunner::onProcessFinished(int exitCode)
{
    emit logMessage(QString("Process exited with code %1").arg(exitCode), exitCode != 0);
}

void ProcessRunner::stop()
{
    if (process.state() == QProcess::Running) {
        process.terminate();
        if (!process.waitForFinished(3000)) {
            process.kill();
        }
    }
}
