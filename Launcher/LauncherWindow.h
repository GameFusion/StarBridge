#pragma once
#include <QMainWindow>
#include <QLabel>
#include <QTextEdit>
#include <QPushButton>
#include <QSystemTrayIcon>
#include <QMenu>
#include <QAction>

#include "ProcessRunner.h"

class LauncherWindow : public QMainWindow
{
    Q_OBJECT
public:
    explicit LauncherWindow(QWidget *parent = nullptr);
    virtual ~LauncherWindow();

protected:
    void closeEvent(QCloseEvent *event) override;

private slots:
    void onPlayClicked();
    void onPauseClicked();
    void onStopClicked();
    void onProcessStateChanged(QProcess::ProcessState state);

    void onShowWindow();
    void onQuit();

    void checkHealth();
    void updateStatus(const QString &text, const QString &color);
private:

    void logMessage(const QString &rawText, bool isError);

    QLabel *statusLabel;
    QTextEdit *logView;
    QPushButton *playBtn;
    QPushButton *pauseBtn;
    QPushButton *stopBtn;

    ProcessRunner *runner;

    int maxLogLines = 20000;     // Max history
    int trimLogLines = 15000;     // Max history
    bool autoFollow = false;       // Auto-scroll when new logs arrive

    QTimer *healthTimer;

    // System Tray variables
    QSystemTrayIcon *trayIcon;
    QMenu *trayMenu;
    QAction *showAction;
    QAction *quitAction;

    enum {WTS, Play, Stop, Pause} State;
};

