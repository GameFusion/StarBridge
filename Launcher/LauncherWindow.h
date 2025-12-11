#pragma once
#include <QMainWindow>
#include <QTextEdit>
#include "ProcessRunner.h"

class LauncherWindow : public QMainWindow
{
    Q_OBJECT
public:
    explicit LauncherWindow(QWidget *parent = nullptr);

    void closeEvent(QCloseEvent *event);

private:
    QTextEdit *logView;
    ProcessRunner *runner;
    //QProcess *pythonProcess;
};

