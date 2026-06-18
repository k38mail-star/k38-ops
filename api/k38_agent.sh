#!/bin/bash
# K38 node agent - prints all metrics as JSON on stdout
uname_s=$(uname)
if [ "$uname_s" = "Darwin" ]; then
  cpu=$(top -l 1 -n 0 2>/dev/null | grep 'CPU usage' | awk '{print $7}')
  [ -z "$cpu" ] && cpu="--"
  mem=$(vm_stat | python3 -c "
import sys,re
d=sys.stdin.read()
a=int(re.search(r'Pages active:\s+(\d+)',d).group(1))
w=int(re.search(r'Pages wired down:\s+(\d+)',d).group(1))
f=int(re.search(r'Pages free:\s+(\d+)',d).group(1))
ia=int(re.search(r'Pages inactive:\s+(\d+)',d).group(1))
print(f'{(a+w)*100//(a+w+f+ia)}%')
" 2>/dev/null)
  [ -z "$mem" ] && mem="--"
else
  cpu=$(python3 -c "
import re
d=open('/proc/stat').readline()
p=d.split()
t=sum(int(x) for x in p[1:])
print(f'{(t-int(p[4]))*100//t}%')
" 2>/dev/null)
  [ -z "$cpu" ] && cpu="--"
  mem=$(python3 -c "
import re
d=open('/proc/meminfo').read()
t=int(re.search(r'MemTotal:\s+(\d+)',d).group(1))
a=int(re.search(r'MemAvailable:\s+(\d+)',d).group(1))
print(f'{(t-a)*100//t}%')
" 2>/dev/null)
  [ -z "$mem" ] && mem="--"
fi
disk=$(df -h / | tail -1 | awk '{print $5}')
uptime_p=$(uptime -p 2>/dev/null || uptime)
echo "{\"cpu\":\"$cpu\",\"mem\":\"$mem\",\"disk\":\"$disk\",\"uptime\":\"$uptime_p\"}"
