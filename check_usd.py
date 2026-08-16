from main import format_usd_futures, format_message, fetch_all, fetch_market_data_json

out = []
out.append('---/usd---')
out.append(format_usd_futures())
out.append('\n---scheduled---')
d = fetch_all()
m = fetch_market_data_json()
out.append(format_message(d, m))
open('check_usd_output.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('WROTE check_usd_output.txt')
