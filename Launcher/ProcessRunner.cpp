#include "ProcessRunner.h"
#include <QDir>
#include <QDebug>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QTimer>

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
    QString basePath = QDir::currentPath();

    // === Read port from .env ===
    QFile envFile(basePath + "/.env");
    if (envFile.open(QIODevice::ReadOnly)) {
        QTextStream in(&envFile);
        while (!in.atEnd()) {
            QString line = in.readLine().trimmed();
            if (line.startsWith("STARBRIDGE_PORT=")) {
                port = line.mid(16).trimmed();
                emit logMessage("Using port from .env: " + port, false);
                break;
            }
        }
        envFile.close();
    }

    QString healthUrl = QString("https://127.0.0.1:%1/health").arg(port);
    QString killUrl = QString("https://127.0.0.1:%1/kill").arg(port);

    emit logMessage(QString("Checking StarBridge on https://127.0.0.1:%1/health").arg(port), false);

    QNetworkAccessManager *manager = new QNetworkAccessManager(this);

    // === ELITE: Ignore SSL errors (self-signed / adhoc) ===
    connect(manager, &QNetworkAccessManager::sslErrors, this, [](QNetworkReply *reply, const QList<QSslError> &errors) {
        reply->ignoreSslErrors();
    });

    QNetworkRequest request(healthUrl);
    //request.setAttribute(QNetworkRequest::FollowRedirectsAttribute, true);

    auto *reply = manager->get(request);

    // === ELITE: Fast timeout (3 seconds) ===
    QTimer::singleShot(3000, reply, [reply]() {
        if (!reply->isFinished()) {
            reply->abort();
        }
    });

    connect(reply, &QNetworkReply::finished, this, [this, reply, killUrl, manager]() {
        if (reply->error() == QNetworkReply::NoError) {
            emit logMessage("StarBridge is running — terminating old instance...", false);

            QNetworkAccessManager *killManager = new QNetworkAccessManager(this);
            connect(killManager, &QNetworkAccessManager::sslErrors, [](QNetworkReply *r, const QList<QSslError>&) {
                r->ignoreSslErrors();
            });

            QNetworkRequest killReq(killUrl);
            auto *killReply = killManager->get(killReq);

            QTimer::singleShot(3000, killReply, [killReply]() { killReply->abort(); });

            connect(killReply, &QNetworkReply::finished, this, [this, killReply, manager]() {
                if (killReply->error() == QNetworkReply::NoError) {
                    emit logMessage("Old instance terminated — waiting for port to free...", false);
                    waitAndStart(port, manager);  // ← ELITE WAIT LOOP
                } else {
                    emit logMessage("Could not reach /kill — forcing start", true);
                    startProcess();
                }
                killReply->deleteLater();
            });

        } else if (reply->error() == QNetworkReply::OperationCanceledError ||
                   reply->error() == QNetworkReply::ConnectionRefusedError ||
                   reply->error() == QNetworkReply::HostNotFoundError) {
            emit logMessage("No running instance — starting fresh", false);
            startProcess();
        } else {
            emit logMessage(QString("Health check error: %1 — starting anyway").arg(reply->errorString()), true);
            startProcess();
        }
        reply->deleteLater();
    });
}

void ProcessRunner::waitAndStart(QString port, QNetworkAccessManager *manager)
{
    QTimer *timer = new QTimer(this);
    int attempt = 0;

    timer->start(1000); // check every 1 second

    int maxAttempts = 3;
    connect(timer, &QTimer::timeout, this, [this, timer, port, maxAttempts, attempt, manager]() mutable {
        attempt++;

        QNetworkAccessManager *manager = new QNetworkAccessManager(this);
        connect(manager, &QNetworkAccessManager::sslErrors,
                [](QNetworkReply *r, const QList<QSslError>&) { r->ignoreSslErrors(); });

        QNetworkRequest req(QString("https://127.0.0.1:%1/health").arg(port));
        auto *reply = manager->get(req);

        // Fast timeout
        QTimer::singleShot(1500, reply, [reply]() { if (!reply->isFinished()) reply->abort(); });

        connect(reply, &QNetworkReply::finished, this, [this, timer, reply, port, attempt, maxAttempts, manager]() {
            bool portFree = (reply->error() != QNetworkReply::NoError);

            if (portFree || attempt >= maxAttempts) {
                timer->stop();
                timer->deleteLater();

                if (portFree) {
                    emit logMessage(QString("Port %1 is free — starting StarBridge").arg(port), false);
                    startProcess();
                } else {
                    emit logMessage(QString("Port %1 still busy after %2s — forcing start anyway").arg(port).arg(maxAttempts), true);
                    startProcess();
                }
            } else {
                emit logMessage(QString("Waiting for port %1 to free... (%2/%3)").arg(port).arg(attempt).arg(maxAttempts), false);
            }
            reply->deleteLater();
            manager->deleteLater();
        });
    });

    // Initial message
    emit logMessage(QString("Waiting up to %1s for port %2 to become free...").arg(maxAttempts).arg(port), false);
}

void ProcessRunner::startProcess()
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
