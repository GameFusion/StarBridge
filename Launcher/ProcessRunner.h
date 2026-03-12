#pragma once
#include <QObject>
#include <QProcess>
#include <QStringList>

class QNetworkAccessManager;

class ProcessRunner : public QObject
{
    Q_OBJECT
public:
    explicit ProcessRunner(QObject *parent = nullptr);
    void startStarBridge();
    void stop();
    bool isLaunchInProgress() const { return launchInProgress; }

    QProcess &getProcess(){
        return process;
    }
    const QString getPort(){
        return port;
    }
signals:
    void logMessage(const QString &msg, bool isError = false);

private slots:
    void onReadyReadStandardOutput();
    void onReadyReadStandardError();
    void onProcessFinished(int exitCode);

private:
    void handleOutput(const QString &output);
    void waitAndStart(QString port, QNetworkAccessManager *manager);
    void startProcess();
    bool sendShutdownRequest(const QStringList &urls, int timeoutMs = 2000);

    QProcess process;

    QString port = "5001";
    QString adminPort = "5002";
    bool launchInProgress = false;
};
