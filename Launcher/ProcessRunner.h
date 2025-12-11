#pragma once
#include <QObject>
#include <QProcess>

class ProcessRunner : public QObject
{
    Q_OBJECT
public:
    explicit ProcessRunner(QObject *parent = nullptr);
    void startStarBridge();
    void stop();

    QProcess &getProcess(){
        return process;
    }
signals:
    void logMessage(const QString &msg, bool isError = false);

private slots:
    void onReadyReadStandardOutput();
    void onReadyReadStandardError();
    void onProcessFinished(int exitCode);

private:
    void handleOutput(const QString &output);

    QProcess process;
};
